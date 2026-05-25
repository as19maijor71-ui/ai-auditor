import asyncio
import base64
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from auditor.bot.storage import SQLiteStorage
from auditor.config import settings
from auditor.engine.cleaner import clean_wb_text
from auditor.engine.excel_parser import ExportParseError, parse_export_file, product_to_text
from auditor.engine.generator import AuditReport, audit_card, call_vision
from auditor.engine.url_fetcher import detect_platform, extract_product_text, fetch_product_page

router = Router()

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 4096

_storage_instance: SQLiteStorage | None = None

_export_cache: dict[int, list] = {}
_suppl_debounce: dict[int, asyncio.Task] = {}


def set_storage(storage: SQLiteStorage) -> None:
    global _storage_instance
    _storage_instance = storage


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _check_audit_limit(user_id: int) -> str | None:
    if _storage_instance is None:
        return None
    if _storage_instance.is_whitelisted(user_id):
        return None
    if _storage_instance.has_free_audits(user_id):
        return None
    return (
        "⚠️ <b>Лимит бесплатных аудитов исчерпан.</b>\n\n"
        f"Ты использовал {_storage_instance.get_usage(user_id)} из {settings.FREE_AUDIT_LIMIT}.\n\n"
        f"Подписка: {settings.SUBSCRIPTION_PRICE}₽/мес — неограниченные аудиты.\n"
        + (f'💬 <a href="https://t.me/{settings.SUPPORT_CHANNEL.lstrip("@")}">Написать в поддержку</a>' if settings.SUPPORT_CHANNEL else "💬 Скоро появится оплата.")
    )


def _safe_send(text: str) -> list[str]:
    chunks: list[str] = []
    while len(text) > TELEGRAM_MAX_LENGTH:
        split_at = text.rfind("\n", 0, TELEGRAM_MAX_LENGTH)
        if split_at == -1:
            split_at = TELEGRAM_MAX_LENGTH
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


class AuditFlow(StatesGroup):
    waiting_url = State()
    collecting_screenshots = State()
    auditing = State()
    choosing_product = State()
    supplementing_export = State()


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    logger.info("User %d (@%s) started the bot", message.from_user.id, message.from_user.username or "?")

    footer_parts = []
    if settings.SUPPORT_CHANNEL:
        ch = settings.SUPPORT_CHANNEL.lstrip("@")
        footer_parts.append(f'💬 <a href="https://t.me/{ch}">Поддержка</a>')
    if settings.PRIVACY_URL:
        footer_parts.append(f'🔒 <a href="{settings.PRIVACY_URL}">Privacy</a>')
    footer = " | ".join(footer_parts)
    start_footer = f"📖 /help | {footer}" if footer else "📖 /help"

    if _storage_instance is not None and not _storage_instance.is_whitelisted(message.from_user.id):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="📩 Запросить доступ",
                    callback_data=f"wl_req:{message.from_user.id}"
                )]
            ]
        )
        await message.answer(
            "🔒 Бот в закрытом тестировании.\n\n"
            f"Твой Telegram ID: <code>{message.from_user.id}</code>\n\n"
            "Нажми кнопку ниже, чтобы запросить доступ.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    current_state = await state.get_state()
    if current_state == AuditFlow.auditing:
        await message.answer("⏳ Аудит уже идёт. Подожди, пожалуйста.")
        return

    await state.clear()
    await state.set_state(AuditFlow.waiting_url)
    start_caption = (
        "👋 <b>Привет! Я — AI-аудитор карточек WB и Ozon</b>\n\n"
        "🔥 Узнай, почему карточка не продаёт — и как это исправить\n\n"
        "Анализирую 5 блоков: заголовок, фото, описание, SEO, конкуренты\n"
        "Приоритеты: 🔴 срочно, 🟡 важно, 🟢 желательно\n\n"
        "📊 <b>Экспорт из ЛК</b> — загрузи XLSX/CSV из WB или Ozon\n"
        "📦 <b>WB</b> — гид копирования (5 шагов по вкладкам карточки)\n"
        "🛒 <b>Ozon</b> — скопируй текст карточки и отправь\n"
        "📸 <b>Скриншоты</b> — для чужих карточек\n\n"
        "⚡ 3 бесплатных аудита — попробуй сейчас!\n\n"
        f"{start_footer}"
    )
    start_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Загрузить экспорт WB/Ozon (XLSX/CSV)", callback_data="how_export")],
            [InlineKeyboardButton(text="📦 WB — гид копирования (5 шагов)", callback_data="start_guided")],
            [InlineKeyboardButton(text="🛒 Ozon — скопировать текст", callback_data="how_ozon")],
            [InlineKeyboardButton(text="📸 Скриншоты", callback_data="how_screenshots")],
            [InlineKeyboardButton(text="📖 Как пользоваться", callback_data="how_help")],
        ]
    )
    try:
        await message.answer_video(
            video="BAACAgIAAxkDAAIBpWoRpQlbuS1l6rjDVYRo3GTIzNNEAALanAAC8xaQSOfkk1YK5sz2OwQ",
            width=800,
            height=450,
            caption=start_caption,
            reply_markup=start_kb,
            parse_mode="HTML",
        )
    except Exception:
        await message.answer(start_caption, reply_markup=start_kb, parse_mode="HTML")


@router.callback_query(F.data == "how_export")
async def how_export_cb(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "<b>📊 Аудит по экспорту из личного кабинета</b>\n\n"
        "<b>Wildberries:</b>\n"
        "1. ЛК → «Товары» → отметить нужные → «Массовое редактирование»\n"
        "2. «Выгрузить в Excel»\n"
        "3. Отправь файл боту\n\n"
        "<b>Ozon:</b>\n"
        "1. ЛК → «Товары и цены» → «Экспорт»\n"
        "2. Выбери формат XLSX или CSV\n"
        "3. Отправь файл боту\n\n"
        "Бот прочитает файл, покажет список товаров — выбери нужный для аудита.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "how_ozon")
async def how_ozon_cb(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "<b>🛒 Ozon — скопируй текст карточки</b>\n\n"
        "1. Открой карточку товара на Ozon\n"
        "2. Скопируй всё что видишь: название, цену, описание, характеристики\n"
        "3. Отправь текст сюда\n\n"
        "Бот проанализирует и выдаст отчёт.\n\n"
        "<i>Автозагрузка по ссылке временно недоступна — Ozon блокирует серверные запросы.</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "how_screenshots")
async def how_screenshots_cb(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "<b>📸 Аудит по скриншотам</b>\n\n"
        "Подходит если нет ссылки Ozon и карточка не ваша.\n\n"
        "1. Нажми <b>«▶️ Начать аудит WB»</b>\n"
        "2. На шаге 4 отправь скриншоты фото товара\n"
        "3. Или в любой момент отправь скриншот — бот распознает текст\n\n"
        "<i>На компьютере: Win+Shift+S → выдели область → Ctrl+V в чат</i>\n"
        "<i>На телефоне: громкость↓ + питание одновременно</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "how_help")
async def how_help_cb(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "<b>📖 Справка</b>\n\n"
        "<b>Что делает бот:</b>\n"
        "Анализирует карточку товара на WB/Ozon по 5 блокам: "
        "заголовок, фото/видео, описание, SEO, конкуренты. "
        "Выдаёт отчёт с приоритетами: 🔴 срочно, 🟡 важно, 🟢 желательно.\n\n"
        "<b>Способы аудита:</b>\n"
        "• WB — гид копирования (5 шагов по вкладкам карточки)\n"
        "• Ozon — скопируй текст карточки и отправь\n"
        "• Скриншоты — для чужих карточек\n\n"
        "<b>Ограничения:</b>\n"
        "• 3 бесплатных аудита\n"
        "• Автозагрузка по ссылке недоступна (блокировка WB/Ozon)\n"
        "• Кнопки видны только в приложении Telegram, не в Web-версии\n\n"
        "<b>Поддержка:</b> " + (f'<a href="https://t.me/{settings.SUPPORT_CHANNEL.lstrip("@")}">канал поддержки</a>' if settings.SUPPORT_CHANNEL else "скоро появится канал поддержки") + "\n\n"
        "<i>Вернуться в начало — /start</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "back_to_start")
async def back_to_start_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AuditFlow.waiting_url)
    await callback.message.answer(
        "👋 <b>AI-аудитор WB и Ozon</b>\n\n"
        "<b>Выбери способ:</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📦 WB — гид копирования", callback_data="start_guided")],
                [InlineKeyboardButton(text="🛒 Ozon — скопировать текст", callback_data="how_ozon")],
                [InlineKeyboardButton(text="📸 Скриншоты (если нет ссылки)", callback_data="how_screenshots")],
                [InlineKeyboardButton(text="📖 Как пользоваться", callback_data="how_help")],
            ]
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "start_guided")
async def start_guided_audit_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await start_guided_audit(callback.message, state)
    await callback.answer()


@router.message(AuditFlow.waiting_url, ~F.photo, ~F.document, ~F.video)
async def url_received(message: Message, state: FSMContext) -> None:
    text = message.text.strip() if message.text else ""
    if len(text) < 10:
        await message.answer("⚠️ Слишком коротко. Отправь скриншот или скопируй текст карточки целиком.")
        return

    platform: str | None = None
    url = ""
    competitor_input = ""

    if text.startswith("http"):
        platform = detect_platform(text)
        if platform:
            url = text
        else:
            await message.answer(
                "❌ Это не ссылка на товар WB или Ozon.\n"
                "Поддерживаются:\n"
                "• wildberries.ru/catalog/...\n"
                "• ozon.ru/product/...\n\n"
                "Или скопируй текст карточки и отправь."
            )
            return

        # URL path: immediate fetch and audit
        limit_msg = _check_audit_limit(message.from_user.id)
        if limit_msg:
            await message.answer(limit_msg, parse_mode="HTML")
            return

        await state.set_state(AuditFlow.auditing)
        thinking_msg = await message.answer(
            "✅ Ссылка получена\n\n"
            "🔍 Загружаю карточку...\n\n"
            "⏳ Это займёт до 2 минут.\n"
            "⚠️ <b>Не закрывайте чат.</b>",
            parse_mode="HTML",
        )
        animation_task = asyncio.create_task(_animate_thinking(thinking_msg))
        try:
            result = await _do_audit_url(url, platform, message)
        except Exception as e:
            logger.warning(f"Audit failed: {e}")
            result = None

        animation_task.cancel()
        try:
            await thinking_msg.delete()
        except TelegramBadRequest:
            pass

        if result:
            await send_audit_report(message, result)
        else:
            await message.answer(
                "⚠️ Не удалось загрузить карточку.\n\n"
                "WB и Ozon часто блокируют автоматическую загрузку.\n\n"
                "<b>Скопируй текст карточки вручную и отправь сюда.</b>\n"
                "Я проанализирую точно так же.",
                parse_mode="HTML",
            )
        await state.set_state(AuditFlow.waiting_url)
    else:
        # Text path: enter collection mode for photos + text
        await state.set_state(AuditFlow.collecting_screenshots)
        await state.update_data(accumulated_text=clean_wb_text(text), current_step=2)
        await message.answer(
            f"✅ Принято. <b>Шаг 2 из {len(GUIDED_STEPS)}</b>\n\n"
            + GUIDED_STEPS[1],
            reply_markup=_guided_menu(),
            parse_mode="HTML",
        )
        return


GUIDED_STEPS = [
    "📋 Скопируй <b>заголовок и цену</b> товара — отправь сюда.",
    "📋 Открой вкладку <b>«О товаре»</b> → скопируй всё (описание + характеристики) → отправь сюда.",
    "📋 Скопируй <b>3-5 отзывов</b> покупателей (без ответов продавца) → отправь сюда.",
    "📸 Скопируй <b>фото товара</b> (ПКМ → Копировать → вставить). Опиши видео если есть.",
    "✅ Всё готово — нажми <b>«Запустить аудит»</b>",
]


def _guided_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Запустить аудит", callback_data="guide_audit")],
            [InlineKeyboardButton(text="🔄 Начать заново", callback_data="guide_reset")],
        ],
    )


async def start_guided_audit(message: Message, state: FSMContext) -> None:
    await state.set_state(AuditFlow.collecting_screenshots)
    await state.update_data(accumulated_text="", current_step=1)
    await message.answer(
        "🚀 <b>Гид копирования WB</b>\n\n"
        "<b>Шаг 1 из 5</b>\n\n"
        + GUIDED_STEPS[0]
        + "\n\n<i>Бот автоматически очистит текст от меню и мусора.</i>",
        reply_markup=_guided_menu(),
        parse_mode="HTML",
    )


@router.message(AuditFlow.waiting_url, F.video)
@router.message(AuditFlow.waiting_url, F.animation)
async def unsupported_media(message: Message) -> None:
    await message.answer(
        "⚠️ Бот принимает текст, файлы экспорта (XLSX/CSV) и скриншоты (фото).\n"
        "Видео и GIF не поддерживаются."
    )


@router.message(AuditFlow.waiting_url, F.photo)
async def first_photo_received(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.set_state(AuditFlow.collecting_screenshots)

    thinking_msg = await message.answer(
        "📸 Скриншот получен\n🔍 Распознаю...\n⚠️ <b>Не закрывайте чат.</b>",
        parse_mode="HTML",
    )

    ocr_text = ""
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_bytes = await bot.download_file(file.file_path)
        image_data = image_bytes.read()
        from auditor.templates.prompts import OCR_PROMPT
        ocr_text = await call_vision(image_data, OCR_PROMPT)
    except Exception as e:
        logger.warning(f"OCR failed: {e}")

    await thinking_msg.delete()

    if not ocr_text or len(ocr_text.strip()) < 20:
        await message.answer(
            "⚠️ Не удалось распознать текст. Попробуй другой скриншот или отправь текст вручную.",
        )
        await state.set_state(AuditFlow.waiting_url)
        return

    await state.update_data(accumulated_text=ocr_text, current_step=1)
    await message.answer(
        f"📸 Фото принято.\n\n"
        + GUIDED_STEPS[0]
        + "\n\nКогда всё — нажми <b>✅ Запустить аудит</b> внизу.",
        reply_markup=_guided_menu(),
        parse_mode="HTML",
    )


@router.message(AuditFlow.collecting_screenshots, F.photo)
async def more_photos_received(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    accumulated = data.get("accumulated_text", "")

    thinking_msg = await message.answer(
        "📸 Скриншот получен\n🔍 Распознаю...\n⚠️ <b>Не закрывайте чат.</b>",
        parse_mode="HTML",
    )

    ocr_text = ""
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_bytes = await bot.download_file(file.file_path)
        image_data = image_bytes.read()
        from auditor.templates.prompts import OCR_PROMPT
        ocr_text = await call_vision(image_data, OCR_PROMPT)
    except Exception as e:
        logger.warning(f"OCR failed on additional photo: {e}")

    await thinking_msg.delete()

    if not ocr_text or len(ocr_text.strip()) < 10:
        await message.answer(
            "⚠️ Не удалось распознать. Попробуй ещё раз или нажми кнопку.",
            reply_markup=_guided_menu(),
        )
        return

    accumulated = accumulated + "\n---\n" + ocr_text
    if len(accumulated) > 6000:
        accumulated = accumulated[:6000]

    photos_done = data.get("photos_done", False)
    if not photos_done:
        await state.update_data(accumulated_text=accumulated, photos_done=True, current_step=5)
        await message.answer(
            f"📸 Фото принято.\n\n"
            + GUIDED_STEPS[4]
            + "\n\nОтправь ещё или нажми <b>✅ Запустить аудит</b> внизу.",
            reply_markup=_guided_menu(),
            parse_mode="HTML",
        )
    else:
        await state.update_data(accumulated_text=accumulated)
        await message.answer(
            f"📸 Фото принято. Когда всё — нажми <b>✅ Запустить аудит</b> внизу.",
            reply_markup=_guided_menu(),
            parse_mode="HTML",
        )


@router.callback_query(F.data == "guide_audit")
async def guide_audit_cb(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    accumulated = data.get("accumulated_text", "")
    if not accumulated or len(accumulated.strip()) < 20:
        await callback.answer("⚠️ Недостаточно данных. Отправь текст.", show_alert=True)
        return
    await callback.message.answer("📊 Запускаю аудит...")
    await _run_full_audit(callback.message, state, accumulated, user_id=callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "guide_reset")
async def guide_reset_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AuditFlow.waiting_url)
    await callback.message.answer("🔄 Начинаем заново. Отправь текст карточки или выбери способ.")
    await callback.answer()


@router.message(AuditFlow.collecting_screenshots, F.text)
async def text_in_collection(message: Message, state: FSMContext) -> None:
    text = message.text.strip() if message.text else ""

    # If user pasted an Ozon share link, process it as a URL
    from auditor.engine.url_fetcher import detect_platform
    url_match = ""
    for word in text.split():
        if word.startswith("http") and detect_platform(word):
            url_match = word
            break
    if url_match:
        data = await state.get_data()
        accumulated = data.get("accumulated_text", "")
        limit_msg = _check_audit_limit(message.from_user.id)
        if limit_msg:
            await message.answer(limit_msg, parse_mode="HTML")
            return
        await state.set_state(AuditFlow.auditing)
        await message.answer("🔗 Ссылка Ozon. Загружаю данные...")
        result = await _do_audit_url(url_match, "ozon", message)
        if result:
            await send_audit_report(message, result)
        else:
            await message.answer("⚠️ Не удалось загрузить карточку по ссылке. Попробуй скриншот или текст.")
        await state.set_state(AuditFlow.waiting_url)
        return

    data = await state.get_data()
    accumulated = data.get("accumulated_text", "")
    current_step = data.get("current_step", 1)

    # Prevent double-processing of the same message
    last_msg_id = data.get("last_msg_id", 0)
    if message.message_id == last_msg_id:
        return
    await state.update_data(last_msg_id=message.message_id)

    # Detect Telegram message splits (multiple messages within 2 seconds)
    import time as _time
    now = _time.time()
    last_m = data.get("last_msg_time", 0)
    is_split = last_m > 0 and (now - last_m) < 2.0
    await state.update_data(last_msg_time=now)

    cleaned = clean_wb_text(text)
    accumulated = accumulated + "\n---\n" + cleaned
    if len(accumulated) > 6000:
        accumulated = accumulated[:6000]

    if is_split:
        await state.update_data(accumulated_text=accumulated)
        return

    next_step = current_step + 1
    await state.update_data(accumulated_text=accumulated, current_step=next_step)

    if next_step > len(GUIDED_STEPS):
        await message.answer("✅ Все шаги пройдены. Запускаю аудит...")
        await _run_full_audit(message, state, accumulated)
    else:
        await message.answer(
            f"✅ Принято. <b>Шаг {next_step} из {len(GUIDED_STEPS)}</b>\n\n"
            + GUIDED_STEPS[next_step - 1],
            reply_markup=_guided_menu(),
            parse_mode="HTML",
        )


async def _run_full_audit(message: Message, state: FSMContext, text: str, user_id: int = 0) -> None:
    uid = user_id or message.from_user.id
    limit_msg = _check_audit_limit(uid)
    if limit_msg:
        await message.answer(limit_msg, parse_mode="HTML")
        await state.set_state(AuditFlow.waiting_url)
        return

    await state.set_state(AuditFlow.auditing)

    thinking_msg = await message.answer(
        "🔍 Полный аудит...\n\n"
        "⏳ Это займёт до 2 минут.\n"
        "⚠️ <b>Не закрывайте чат.</b>",
        parse_mode="HTML",
    )

    animation_task = asyncio.create_task(_animate_thinking(thinking_msg))

    try:
        result = await _do_audit_text(text, message, uid)
    except Exception as e:
        logger.warning(f"Audit failed: {e}")
        result = None

    animation_task.cancel()
    try:
        await thinking_msg.delete()
    except Exception:
        pass

    if result:
        await send_audit_report(message, result)
    else:
        await message.answer(
            "⚠️ Не удалось выполнить аудит. Попробуй ещё раз.",
            parse_mode="HTML",
        )

    await state.set_state(AuditFlow.waiting_url)


async def _do_audit_url(url: str, platform: str, message: Message) -> AuditReport | None:
    try:
        html_content = await fetch_product_page(url)
        product_text = extract_product_text(html_content, platform)
        if not product_text:
            await message.answer("⚠️ Не удалось извлечь данные карточки. Попробуйте другую ссылку.")
            return None

        report = await audit_card(product_text, url, platform)
        _log_audit(message.from_user.id, message.from_user.username, url, platform, report.overall_score)
        return report
    except Exception as e:
        logger.warning(f"URL audit failed: {e}")
        return None


async def _do_audit_text(text: str, message: Message, user_id: int = 0) -> AuditReport | None:
    try:
        report = await audit_card(text[-8000:], "", "manual")
        _log_audit(user_id or message.from_user.id, message.from_user.username, "", "manual", report.overall_score)
        return report
    except Exception as e:
        logger.warning(f"Text audit failed: {e}")
        return None


def _log_audit(user_id: int, username: str | None, url: str, platform: str, score: int) -> None:
    if _storage_instance is not None:
        _storage_instance.log_audit(
            user_id,
            username or "",
            url or "manual_input",
            platform,
            score,
        )
        _storage_instance.increment_usage(user_id)


async def send_audit_report(message: Message, report: AuditReport) -> None:
    if report.overall_score:
        filled = min(10, max(1, report.overall_score // 10))
        empty = 10 - filled
        stars = "⭐" * filled + "☆" * empty
        await message.answer(
            f"<b>📊 ОБЩАЯ ОЦЕНКА КАРТОЧКИ: {report.overall_score}/100 {stars}</b>",
            parse_mode="HTML",
        )

    sections = {"title": "📝 ЗАГОЛОВОК", "photos": "📸 ФОТО/ВИДЕО", "description": "📄 ОПИСАНИЕ",
                "seo": "🔍 SEO", "competitors": "🕵️ КОНКУРЕНТЫ"}

    for section_key, section_title in sections.items():
        section_items = [i for i in report.items if i.section == section_key]
        if not section_items:
            continue

        priority_icon = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
        lines = [f"{section_title} — {_section_score(report, section_key)}"]
        for item in section_items:
            icon = priority_icon.get(item.priority, "⚪")
            lines.append(f"\n{icon} {_escape(item.finding)}")
            lines.append(f"   💡 {_escape(item.recommendation)}")
            lines.append(f"   ❓ {_escape(item.why)}")

        text = "\n".join(lines)
        for chunk in _safe_send(text):
            try:
                await message.answer(chunk, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to send audit chunk: {e}")
                await message.answer(_escape(chunk))

    if report.summary:
        await message.answer(f"💬 <b>Итог:</b>\n{_escape(report.summary)}", parse_mode="HTML")

    if report.competitor_insight:
        await message.answer(f"🕵️ <b>Конкуренты:</b>\n{_escape(report.competitor_insight)}", parse_mode="HTML")

    summary_text = _format_audit_for_copy(report)
    copy_key = f"{message.from_user.id}:{id(summary_text)}"
    if _storage_instance is not None:
        _storage_instance.store_copy_data(copy_key, summary_text)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Копировать отчёт", callback_data=f"copy_audit:{copy_key}")]
        ]
    )
    await message.answer(
        "📋 Нажми, чтобы скопировать отчёт целиком.",
        reply_markup=keyboard,
    )


def _section_score(report: AuditReport, section: str) -> str:
    items = [i for i in report.items if i.section == section]
    if not items:
        return ""
    reds = sum(1 for i in items if i.priority == "red")
    yellows = sum(1 for i in items if i.priority == "yellow")
    if reds == 0 and yellows == 0:
        return "✅"
    return f"{reds}🔴 {yellows}🟡"


def _format_audit_for_copy(report: AuditReport) -> str:
    sections = {"title": "=== ЗАГОЛОВОК ===", "photos": "=== ФОТО/ВИДЕО ===",
                "description": "=== ОПИСАНИЕ ===", "seo": "=== SEO ===", "competitors": "=== КОНКУРЕНТЫ ==="}
    parts = [f"AI-АУДИТ КАРТОЧКИ\n{report.url}\n"]
    if report.overall_score:
        parts.insert(0, f"=== ОБЩАЯ ОЦЕНКА: {report.overall_score}/100 ===")
    for key, title in sections.items():
        items = [i for i in report.items if i.section == key]
        if not items:
            continue
        parts.append(title)
        for item in items:
            icon = {"red": "КРИТИЧНО", "yellow": "ВАЖНО", "green": "ЖЕЛАТЕЛЬНО"}.get(item.priority, "")
            parts.append(f"[{icon}] {item.finding}")
            parts.append(f"Исправить: {item.recommendation}")
            parts.append(f"Почему: {item.why}")
            parts.append("")
    if report.summary:
        parts.append(f"ИТОГ: {report.summary}")
    return "\n".join(parts)


async def _animate_thinking(msg: Message) -> None:
    phases = [
        "🔍 Загружаю карточку...",
        "🤖 Анализирую заголовок и фото...",
        "📄 Проверяю описание...",
        "🔎 Ищу ошибки SEO...",
        "🕵️ Сравниваю с конкурентами...",
        "📝 Формирую отчёт...",
    ]
    i = 0
    prefix = "⏳"
    while True:
        try:
            phase_text = phases[i % len(phases)]
            dots = "." * ((i // len(phases) + 1) % 4)
            await msg.edit_text(
                f"{prefix} Анализ карточки\n\n"
                f"{phase_text}\n"
                f"▸ Шаг {i % len(phases) + 1} из {len(phases)}\n\n"
                f"⚠️ <b>Не закрывайте чат.</b>",
                parse_mode="HTML",
            )
            i += 1
            await asyncio.sleep(1.5)
        except TelegramBadRequest:
            await asyncio.sleep(1.5)


@router.callback_query(F.data.startswith("wl_req:"))
async def access_request(callback: CallbackQuery) -> None:
    if _storage_instance is None:
        await callback.answer("⚠️ Ошибка хранилища")
        return
    user_id_str = callback.data.split(":", 1)[1]
    if not user_id_str.isdigit():
        await callback.answer("⚠️ Некорректный ID")
        return
    user_id = int(user_id_str)
    username = callback.from_user.username or ""
    full_name = callback.from_user.full_name or ""
    encoded = base64.b64encode(username.encode()).decode()
    encoded_fn = base64.b64encode(full_name.encode()).decode()
    admin_id = settings.ADMIN_USER_ID
    if not admin_id:
        await callback.answer("⚠️ Администратор не настроен")
        return
    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"wl_approve:{user_id}:{encoded}:{encoded_fn}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wl_reject:{user_id}"),
            ]
        ]
    )
    try:
        await callback.bot.send_message(
            admin_id,
            f"📩 <b>Запрос доступа</b>\n\n👤 {full_name}\n🆔 <code>{user_id}</code>\n{'📛 @' + username if username else '📛 username скрыт'}",
            reply_markup=admin_kb,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Failed to notify admin: {e}")
        await callback.answer("⚠️ Не удалось отправить запрос")
        return
    await callback.message.edit_text(
        f"🔒 Бот в закрытом тестировании.\n\n✅ Запрос отправлен. Ожидай одобрения.\n\nТвой ID: <code>{user_id}</code>",
        parse_mode="HTML",
    )
    await callback.answer("✅ Запрос отправлен")


@router.callback_query(F.data.startswith("wl_approve:"))
async def approve_access(callback: CallbackQuery) -> None:
    if _storage_instance is None or settings.ADMIN_USER_ID != callback.from_user.id:
        await callback.answer("⛔ Нет прав")
        return
    parts = callback.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await callback.answer("⚠️ Некорректный ID")
        return
    user_id = int(parts[1])
    username = ""
    full_name = ""
    if len(parts) >= 3:
        try:
            username = base64.b64decode(parts[2]).decode()
        except Exception:
            pass
    if len(parts) >= 4:
        try:
            full_name = base64.b64decode(parts[3]).decode()
        except Exception:
            pass
    _storage_instance.add_to_whitelist(user_id, username, full_name, callback.from_user.id)
    await callback.message.edit_text(
        callback.message.html_text + "\n\n✅ <b>Одобрено</b>",
        parse_mode="HTML",
    )
    try:
        await callback.bot.send_message(user_id, "✅ <b>Доступ открыт!</b>\n\nНапиши /start чтобы начать.", parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Failed to notify user {user_id} about approval: {e}")
    await callback.answer("✅ Доступ открыт")


@router.callback_query(F.data.startswith("wl_reject:"))
async def reject_access(callback: CallbackQuery) -> None:
    if _storage_instance is None or settings.ADMIN_USER_ID != callback.from_user.id:
        await callback.answer("⛔ Нет прав")
        return
    user_id_str = callback.data.split(":", 1)[1]
    if not user_id_str.isdigit():
        await callback.answer("⚠️ Некорректный ID")
        return
    user_id = int(user_id_str)
    await callback.message.edit_text(callback.message.html_text + "\n\n❌ <b>Отклонено</b>", parse_mode="HTML")
    try:
        await callback.bot.send_message(user_id, "❌ В доступе отказано.")
    except Exception as e:
        logger.warning(f"Failed to notify user {user_id} about rejection: {e}")
    await callback.answer("❌ Отклонено")


@router.callback_query(F.data.startswith("copy_audit:"))
async def copy_audit_report(callback: CallbackQuery) -> None:
    if _storage_instance is None:
        await callback.answer("⚠️ Отчёт не найден")
        return
    key = callback.data.removeprefix("copy_audit:")
    text = _storage_instance.get_copy_data(key)
    if not text:
        await callback.answer("⚠️ Отчёт устарел")
        return
    escaped = _escape(text)
    await callback.message.edit_text(
        f"<pre>{escaped}</pre>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В главное меню", callback_data="back_to_start")],
        ]),
    )
    await callback.answer("✅ Отчёт скопирован")


@router.message(F.document)
async def export_file_received(message: Message, state: FSMContext, bot: Bot) -> None:
    doc = message.document
    if not doc:
        return

    file_ext = ""
    if doc.file_name:
        file_ext = doc.file_name.rsplit(".", 1)[-1].lower() if "." in doc.file_name else ""

    if file_ext not in ("xlsx", "xls", "csv"):
        await message.answer(
            "⚠️ Неподдерживаемый формат. Загрузите файл экспорта из личного кабинета WB или Ozon (XLSX или CSV)."
        )
        return

    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await message.answer("⚠️ Файл слишком большой. Максимум 5 МБ.")
        return

    await message.answer("📂 Читаю файл экспорта...")

    try:
        file = await bot.get_file(doc.file_id)
        file_bytes = await bot.download_file(file.file_path)
        data = file_bytes.read()
        products = parse_export_file(data, doc.file_name)
    except ExportParseError as e:
        await message.answer(f"⚠️ {e}")
        return
    except Exception as e:
        logger.warning(f"Export parse failed: {e}")
        await message.answer("⚠️ Не удалось прочитать файл. Убедитесь, что это экспорт из ЛК WB или Ozon.")
        return

    from auditor.engine.excel_parser import ExportedProduct
    products: list[ExportedProduct] = products

    platform_name = "WB" if products[0].platform == "wb" else "Ozon"

    if len(products) == 1:
        p = products[0]
        text = product_to_text(p)
        await state.set_state(AuditFlow.waiting_url)
        await _run_full_audit(message, state, text)
        return

    _export_cache[message.from_user.id] = products

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for p in products[:30]:
        display = p.title[:60] + ("..." if len(p.title) > 60 else "")
        if p.sku:
            display = f"[{p.sku}] {display}"[:64]
        keyboard_rows.append([
            InlineKeyboardButton(text=display, callback_data=f"audit_export:{p.row}")
        ])

    await state.set_state(AuditFlow.choosing_product)
    await message.answer(
        f"📊 <b>{platform_name}</b> — найдено {len(products)} товаров.\n\n"
        f"Выбери товар для аудита (показаны первые 30):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("audit_export:"), AuditFlow.choosing_product)
async def audit_export_product(callback: CallbackQuery, state: FSMContext) -> None:
    row_str = callback.data.removeprefix("audit_export:")
    if not row_str.isdigit():
        await callback.answer("⚠️ Некорректный выбор")
        return

    row_num = int(row_str)
    products = _export_cache.get(callback.from_user.id, [])
    product = None
    for p in products:
        if p.row == row_num:
            product = p
            break

    if not product:
        await callback.answer("⚠️ Товар не найден. Загрузите файл заново.")
        return

    limit_msg = _check_audit_limit(callback.from_user.id)
    if limit_msg:
        await callback.message.answer(limit_msg, parse_mode="HTML")
        await state.set_state(AuditFlow.waiting_url)
        await callback.answer()
        return

    text = product_to_text(product)
    missing: list[str] = []
    missing.append("📄 Открой вкладку «О товаре» → скопируй всё (Описание + Характеристики) → отправь сюда")
    missing.append("🖼 ШАГ 1: Скриншот ГЛАВНОЙ страницы карточки (заголовок + цена + рейтинг + первое фото)")
    missing.append("🖼 ШАГ 2: Присылай по ОДНОМУ скриншоту каждого фото из галереи. Бот проанализирует каждое и скажет: оставить, удалить или переместить.")
    missing.append("🎥 ВИДЕО: ОПИШИ видео текстом (длительность, что показано, на какой позиции стоит)")

    await state.set_state(AuditFlow.supplementing_export)
    await state.update_data(
        accumulated_text=text,
        supplement_has_description=bool(product.description),
        supplement_platform=product.platform,
        supplement_base_text=text,
        checklist_last_text="",
        photo_count=0,
        suppl_done=[],
        suppl_items=[{"id": f"item_{i}", "text": m} for i, m in enumerate(missing)],
    )

    present: list[str] = [f"• Название: {product.title[:60]}"]
    if product.brand:
        present.append(f"• Бренд: {product.brand}")
    if product.price:
        present.append(f"• Цена: {product.price} ₽")
    if product.description:
        present.append(f"• Описание: есть ({len(product.description)} симв.)")
    if product.category:
        present.append(f"• Категория: {product.category}")

    missing_text = "\n".join(f"  {m}" for m in missing)
    present_text = "\n".join(present)
    sent = await callback.message.answer(
        f"✅ <b>{product.title[:80]}</b>\n\n"
        f"<b>Есть из экспорта:</b>\n"
        f"{present_text}\n\n"
        f"<b>Добавьте недостающее:</b>\n"
        f"{missing_text}\n\n"
        f"💡 <i>Скрин: Win+Shift+S → выделить → Ctrl+V в чат</i>\n\n"
        f"Когда всё готово — <b>«Запустить аудит»</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Запустить аудит", callback_data="suppl_audit")],
            [InlineKeyboardButton(text="↩️ В главное меню", callback_data="back_to_start")],
        ]),
        parse_mode="HTML",
    )
    await state.update_data(checklist_msg_id=sent.message_id)
    await callback.answer()


@router.message(AuditFlow.supplementing_export, F.text)
async def supplement_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    accumulated = data.get("accumulated_text", "")
    cleaned = clean_wb_text(message.text.strip())
    accumulated = accumulated + "\n---\n" + cleaned
    if len(accumulated) > 6000:
        accumulated = accumulated[:6000]
    await state.update_data(accumulated_text=accumulated)

    data = await state.get_data()
    items = data.get("suppl_items", [])
    done = data.get("suppl_done", [])
    done_id = ""
    for item in items:
        if item["id"] not in done and any(w in item["text"] for w in ["Описание", "Характеристики", "текст", "ВИДЕО", "видео", "ОПИШИ"]):
            done_id = item["id"]
            break

    uid = message.from_user.id
    if uid in _suppl_debounce and not _suppl_debounce[uid].done():
        _suppl_debounce[uid].cancel()
    async def _delayed():
        await asyncio.sleep(2)
        await _update_checklist(message.chat.id, state, message.bot, done_item_id=done_id, just_got="Текст получен.")
    _suppl_debounce[uid] = asyncio.create_task(_delayed())


@router.message(AuditFlow.supplementing_export, F.photo)
async def supplement_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    ocr_text = ""
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_bytes = await bot.download_file(file.file_path)
        image_data = image_bytes.read()
        from auditor.templates.prompts import OCR_PROMPT
        ocr_text = await call_vision(image_data, OCR_PROMPT)
    except Exception as e:
        logger.warning(f"OCR failed: {e}")

    data = await state.get_data()
    pcount = data.get("photo_count", 0) + 1
    await state.update_data(photo_count=pcount)

    if ocr_text and len(ocr_text.strip()) >= 10:
        analysis = data.get("photo_analysis", "") + f"\n\n[ФОТО {pcount}]\n{ocr_text.strip()}"
        await state.update_data(photo_analysis=analysis)

        base_text = data.get("supplement_base_text", "")
        full_text = base_text + f"\n\n=== АНАЛИЗ ФОТО ({pcount} шт.) ==={analysis[:7000]}"
        await state.update_data(accumulated_text=full_text)

    data = await state.get_data()
    items = data.get("suppl_items", [])
    done = data.get("suppl_done", [])
    photo_id = ""
    for item in items:
        if item["id"] not in done and any(w in item["text"] for w in ["ШАГ", "фото", "главной"]):
            photo_id = item["id"]
            break

    await _update_checklist(message.chat.id, state, bot,
                             done_item_id=photo_id,
                             just_got=f"Скриншот {pcount} получен." if ocr_text and len(ocr_text.strip()) >= 10
                             else f"Скриншот {pcount} получен.")


async def _update_checklist(chat_id: int, state: FSMContext, bot: Bot, done_item_id: str = "", just_got: str = "") -> None:
    data = await state.get_data()
    items: list[dict] = data.get("suppl_items", [])
    done: list[str] = data.get("suppl_done", [])

    if done_item_id and done_item_id not in done:
        done.append(done_item_id)
        await state.update_data(suppl_done=done)

    remaining = []
    for item in items:
        if item["id"] in done:
            remaining.append(f"✅ {item['text']}")
        else:
            remaining.append(f"⬜ {item['text']}")

    prefix = f"✅ {just_got}\n\n" if just_got else ""
    if all(item["id"] in done for item in items):
        text = f"{prefix}Всё собрано!"
    else:
        text = f"{prefix}" + "\n".join(remaining)
        if any(item["id"] not in done and any(w in item["text"] for w in ["ШАГ", "фото", "главной"]) for item in items):
            text += "\n\n💡 <i>Скрин: Win+Shift+S → выделить → Ctrl+V в чат</i>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Запустить аудит", callback_data="suppl_audit")],
        [InlineKeyboardButton(text="↩️ В главное меню", callback_data="back_to_start")],
    ])

    try:
        await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await bot.send_message(chat_id, _escape(text), reply_markup=kb)


@router.callback_query(F.data == "suppl_audit")
async def suppl_run_audit(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    accumulated = data.get("accumulated_text", "")
    if not accumulated or len(accumulated.strip()) < 20:
        await callback.answer("⚠️ Недостаточно данных. Отправьте описание или скриншоты.", show_alert=True)
        return
    await callback.message.answer("📊 Запускаю аудит...")
    await _run_full_audit(callback.message, state, accumulated, user_id=callback.from_user.id)
    await callback.answer()


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>📖 AI-аудитор — справка</b>\n\n"
        "<b>Что делает бот:</b>\n"
        "Анализирует карточку товара на WB/Ozon по 5 блокам:\n"
        "📝 Заголовок, 📸 Фото/видео, 📄 Описание, 🔍 SEO, 🕵️ Конкуренты.\n"
        "Выдаёт отчёт с приоритетами: 🔴 срочно, 🟡 важно, 🟢 желательно.\n\n"
        "<b>Как аудитовать Ozon:</b>\n"
        "1. Открой карточку на Ozon\n"
        "2. Скопируй текст: название, цену, описание, характеристики\n"
        "3. Отправь боту\n\n"
        "<b>Как аудитовать Wildberries:</b>\n"
        "1. Нажми кнопку «WB — гид копирования»\n"
        "2. Копируй данные по шагам: заголовок → «О товаре» → отзывы → фото\n"
        "3. Нажми ✅ Запустить аудит\n\n"
        "<b>Как сделать скриншот:</b>\n"
        "• Компьютер: Win+Shift+S → выдели → Ctrl+V в чат\n"
        "• Телефон: громкость↓ + питание одновременно\n\n"
        "<b>Ограничения:</b>\n"
        "• 3 аудита в минуту\n"
        "• Автозагрузка по ссылке недоступна — WB и Ozon блокируют серверные IP\n\n"
        "<b>💬 Поддержка:</b> " + (f'<a href="https://t.me/{settings.SUPPORT_CHANNEL.lstrip("@")}">канал поддержки</a>' if settings.SUPPORT_CHANNEL else "скоро") + "\n"
        + (f'<b>🔒 Конфиденциальность:</b> <a href="{settings.PRIVACY_URL}">политика обработки данных</a>\n\n' if settings.PRIVACY_URL else "")
        + "<i>/start — вернуться в начало</i>",
        parse_mode="HTML",
    )


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if settings.ADMIN_USER_ID == 0:
        await message.answer("⛔ Администратор не настроен.")
        return
    if message.from_user.id != settings.ADMIN_USER_ID:
        await message.answer("⛔ Эта команда только для администратора.")
        return
    if _storage_instance is None:
        await message.answer("⚠️ Хранилище недоступно.")
        return
    wl_users = _storage_instance.get_whitelist_users()
    gen_rows = _storage_instance.get_recent_activity(limit=30)
    lines = ["📊 <b>Статистика</b>\n"]
    lines.append(f"<b>Доступ открыт:</b> {len(wl_users)} чел.")
    for wl in wl_users:
        uid = wl["user_id"]
        full_name = wl.get("full_name") or ""
        username = wl.get("username") or ""
        if uid == settings.ADMIN_USER_ID:
            display = "👑 Организатор"
        elif full_name:
            display = full_name
        elif username and username != "admin":
            display = f"@{username}"
        else:
            display = f"ID:{uid}"
        user_gens = [r for r in gen_rows if r["user_id"] == uid]
        used = _storage_instance.get_usage(uid) if _storage_instance else 0
        remaining = max(0, settings.FREE_AUDIT_LIMIT - used)
        status = f"✅ {len(user_gens)} ауд. (осталось {remaining})" if user_gens else f"⏳ не пользовался (осталось {remaining})"
        lines.append(f"  • {display} — {status}")
    total = len(gen_rows)
    lines.append(f"\nАудитов за 7 дней: {total}")
    if total == 0 and len(wl_users) == 0:
        lines = ["📊 Пока нет данных."]
    await message.answer("\n".join(lines), parse_mode="HTML")
