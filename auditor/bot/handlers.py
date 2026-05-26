import asyncio
import base64
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from auditor.bot.storage import SQLiteStorage
from auditor.bot.paste_collector import (
    append_paste_part,
    build_paste_status_text,
    decode_txt_file,
    is_enough_for_quick_audit,
    is_txt_document,
)
from auditor.config import settings
from auditor.engine.cleaner import clean_wb_text
from auditor.engine.excel_parser import ExportParseError, parse_export_file, product_to_text
from auditor.engine.audit_runner import QuickAuditError, run_quick_text_audit
from auditor.engine.generator import AuditReport, audit_card, call_vision
from auditor.engine.media_models import MediaItem, VideoDescription
from auditor.engine.media_runner import (
    MediaAuditError,
    build_media_audit_items,
    call_gemini_media_audit,
    parse_video_description,
)
from auditor.engine.paste_models import LocalAuditFacts, MarketplaceCardSnapshot
from auditor.engine.paste_parser import (
    build_local_audit_facts,
    parse_marketplace_paste,
    sanitize_personal_data,
)
from auditor.engine.report_exporter import (
    build_report_filename,
    export_audit_report_text,
    extract_top_actions,
)
from auditor.engine.url_fetcher import detect_platform, extract_product_text, fetch_product_page
from auditor.templates.prompts import build_video_description_help_text

router = Router()

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 4096
PASTE_TXT_MAX_BYTES: int = 128 * 1024
REPORT_TEXT_MAX_CHUNKS: int = 3
PRE_TEXT_SAFE_CHUNK_LENGTH: int = (TELEGRAM_MAX_LENGTH - len("<pre></pre>")) // 5

_storage_instance: SQLiteStorage | None = None

_export_cache: dict[int, list] = {}
_suppl_debounce: dict[int, asyncio.Task] = {}


def set_storage(storage: SQLiteStorage) -> None:
    global _storage_instance
    _storage_instance = storage


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


async def _safe_callback_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False) -> None:
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest:
        pass


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


async def _check_media_audit_limit(user_id: int, state: FSMContext) -> str | None:
    data = await state.get_data()
    if (
        data.get("last_audit_user_id") == user_id
        and isinstance(data.get("last_audit_report"), dict)
        and data.get("last_report_media_added") is True
    ):
        return (
            "⚠️ <b>Медиа уже добавлены к этому отчёту.</b>\n\n"
            "Визуальное расширение можно запускать только один раз для одного аудита."
        )

    limit_msg = _check_audit_limit(user_id)
    if not limit_msg:
        return None

    if data.get("last_audit_user_id") == user_id and isinstance(data.get("last_audit_report"), dict):
        return None
    return limit_msg


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


def _safe_send_pre_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    max_length = max(1, PRE_TEXT_SAFE_CHUNK_LENGTH)
    while len(text) > max_length:
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def _paste_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Запустить быстрый аудит", callback_data="paste_run")],
            [InlineKeyboardButton(text="🔄 Начать заново", callback_data="paste_reset")],
            [InlineKeyboardButton(text="↩️ В главное меню", callback_data="back_to_start")],
        ],
    )


def _media_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я отправил все фото", callback_data="media_photos_done")],
            [InlineKeyboardButton(text="🎥 Описать видео", callback_data="media_describe_video")],
            [InlineKeyboardButton(text="↩️ В главное меню", callback_data="back_to_start")],
        ],
    )


def _media_video_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад к фото", callback_data="media_back_to_photos")],
        ],
    )


def _report_actions_keyboard(copy_key: str, media_added: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📋 Копировать отчёт", callback_data=f"copy_audit:{copy_key}")],
    ]
    if not media_added:
        buttons.append(
            [InlineKeyboardButton(text="📸 Добавить фото/видео к отчёту", callback_data="media_next_step")]
        )
    buttons.append([InlineKeyboardButton(text="↩️ В главное меню", callback_data="back_to_start")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_media_start_text() -> str:
    return (
        "📸 <b>Медиа-расширение отчёта</b>\n\n"
        "Отправляй фото галереи по порядку: первое фото станет позицией 1, "
        "второе — позицией 2 и так далее.\n\n"
        f"Максимум: {settings.MEDIA_MAX_PHOTOS} фото. Для MVP достаточно главного фото, "
        "первых 5-7 фото и всех слайдов с инфографикой, составом, упаковкой или размерами.\n\n"
        "Когда фото закончатся, нажми <b>«✅ Я отправил все фото»</b>. "
        "Видео файлом отправлять не нужно — его можно описать текстом по шаблону."
    )


def _media_type_label(media_type: str) -> str:
    return {
        "main": "главное фото",
        "lifestyle": "lifestyle",
        "infographic": "инфографика",
        "composition": "состав/комплектация",
        "packaging": "упаковка",
        "review": "отзыв/UGC",
        "other": "другое",
    }.get(media_type, media_type)


def _media_verdict_label(verdict: str) -> str:
    return {
        "keep": "оставить",
        "remove": "убрать/заменить",
        "move": "переместить",
        "unknown": "проверить вручную",
    }.get(verdict, verdict)


def _video_type_label(video_type: str) -> str:
    return {
        "unboxing": "распаковка",
        "product_review": "обзор товара",
        "product_usage": "использование продукта",
        "before_after": "до/после",
        "recipe_or_instruction": "рецепт или инструкция",
        "customer_review": "отзыв покупателя",
        "slideshow": "слайд-шоу",
        "ad": "реклама",
        "weak_or_unclear": "слабое или непонятное видео",
        "other": "другое",
    }.get(video_type, video_type)


def _build_start_caption(start_footer: str) -> str:
    return (
        "👋 <b>AI-аудитор карточек WB/Ozon</b>\n\n"
        "<b>Загрузи ссылку — получи отчёт.</b> Сейчас самый надёжный путь — "
        "скопировать текст страницы карточки из браузера.\n\n"
        "🧾 <b>Основной способ</b>\n"
        "Нажми кнопку <b>«🧾 Вставить текст карточки»</b> ниже, затем:\n"
        "1. Открой карточку WB/Ozon в браузере.\n"
        "2. Нажми <code>Ctrl+A</code>.\n"
        "3. Нажми <code>Ctrl+C</code>.\n"
        "4. Вставь текст в бот одним или несколькими сообщениями.\n"
        "5. Если Telegram не принимает длинный текст — отправь <code>.txt</code> файл.\n"
        "6. Нажми <b>«✅ Запустить быстрый аудит»</b>.\n\n"
        "⚡ Быстрый аудит проверяет текст и факты карточки без проверки фото/видео.\n"
        "После текстового отчёта можно будет усилить аудит фото/видео отдельным шагом.\n\n"
        "↩️ <b>Запасные/старые способы ниже</b>\n"
        "Excel/CSV, старый WB-гид, Ozon-подсказка и скриншоты сохранены, "
        "но это не основной путь.\n\n"
        "⚡ 3 бесплатных аудита — попробуй сейчас!\n\n"
        f"{start_footer}"
    )


def _build_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧾 Вставить текст карточки", callback_data="paste_start")],
            [InlineKeyboardButton(text="📊 Запасной: экспорт XLSX/CSV", callback_data="how_export")],
            [InlineKeyboardButton(text="📦 Старый WB-гид копирования", callback_data="start_guided")],
            [InlineKeyboardButton(text="🛒 Ozon: общий Ctrl+A/Ctrl+C", callback_data="how_ozon")],
            [InlineKeyboardButton(text="📸 Запасной: скриншоты", callback_data="how_screenshots")],
            [InlineKeyboardButton(text="📖 Как пользоваться", callback_data="how_help")],
        ],
    )


def _build_help_text() -> str:
    return (
        "<b>📖 AI-аудитор — справка</b>\n\n"
        "<b>Основной способ</b>\n"
        "В /start нажми <b>«🧾 Вставить текст карточки»</b>, затем:\n"
        "1. Открой карточку WB/Ozon в браузере.\n"
        "2. Нажми <code>Ctrl+A</code>.\n"
        "3. Нажми <code>Ctrl+C</code>.\n"
        "4. Вставь текст в бот одним или несколькими сообщениями или отправь <code>.txt</code>.\n"
        "5. Нажми <b>«✅ Запустить быстрый аудит»</b>.\n\n"
        "<b>Что проверяет быстрый аудит</b>\n"
        "• заголовок\n"
        "• цена и конкурентная полка\n"
        "• описание\n"
        "• характеристики\n"
        "• SEO\n"
        "• отзывы/риски, если они есть в копипасте\n\n"
        "<b>Что не проверяется без медиа</b>\n"
        "• фото\n"
        "• инфографика\n"
        "• видео\n"
        "• порядок галереи\n\n"
        "<b>Запасные способы</b>\n"
        "• Excel/CSV из личного кабинета, если копипаст неудобен\n"
        "• старый WB guide\n"
        "• скриншоты как запасной ввод или будущий медиа-этап\n\n"
        "Быстрый аудит — это не длинная анкета: достаточно текста страницы и кнопки запуска.\n\n"
        "<i>/start — вернуться в начало</i>"
    )


def _build_help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧾 Вставить текст карточки", callback_data="paste_start")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")],
        ],
    )


def _paste_instruction_text() -> str:
    return (
        "🧾 <b>Вставь текст карточки WB/Ozon</b>\n\n"
        "1. Открой карточку WB/Ozon в браузере.\n"
        "2. Нажми <code>Ctrl+A</code>.\n"
        "3. Нажми <code>Ctrl+C</code>.\n"
        "4. Вставь текст сюда одним или несколькими сообщениями.\n"
        "5. Если Telegram не принимает длинный текст — отправь <code>.txt</code> файл.\n\n"
        "Я накоплю текст и по кнопке ниже запущу быстрый аудит без проверки фото/видео. "
        "Медиа можно будет добавить отдельным шагом после текстового отчёта."
    )


def _format_platform(platform: str) -> str:
    names = {"wb": "Wildberries", "ozon": "Ozon", "unknown": "не определена"}
    return names.get(platform, platform)


def _format_optional(value: object | None) -> str:
    if value is None or value == "":
        return "нет в копипасте"
    return _escape(str(value))


async def _append_paste_to_state(
    state: FSMContext,
    new_part: str,
    source_type: str,
) -> tuple[int, int, bool]:
    data = await state.get_data()
    current_text = str(data.get("paste_text", ""))
    parts_count = int(data.get("paste_parts", 0)) + 1
    was_truncated = bool(data.get("paste_truncated", False))

    paste_text, just_truncated = append_paste_part(current_text, new_part)
    truncated = was_truncated or just_truncated
    await state.update_data(
        paste_text=paste_text,
        paste_parts=parts_count,
        paste_truncated=truncated,
        paste_source_type=source_type,
    )
    return len(paste_text), parts_count, truncated


def _build_paste_preview_text(
    snapshot: MarketplaceCardSnapshot,
    facts: LocalAuditFacts,
) -> str:
    price = f"{snapshot.current_price} ₽" if snapshot.current_price is not None else None
    missing_blocks = ", ".join(snapshot.missing_blocks) if snapshot.missing_blocks else "нет"
    return (
        "🧾 <b>Быстрая локальная проверка</b>\n\n"
        f"• Площадка: {_escape(_format_platform(snapshot.platform))}\n"
        f"• Название: {_format_optional(snapshot.product_name)}\n"
        f"• Цена: {_format_optional(price)}\n"
        f"• Рейтинг: {_format_optional(snapshot.rating)}\n"
        f"• Отзывы: {_format_optional(snapshot.review_count)}\n"
        f"• Характеристик: {facts.characteristics_count}\n"
        f"• Конкурентов: {facts.competitors_count}\n"
        f"• missing_blocks: {_escape(missing_blocks)}\n\n"
        "Данные собраны. AI-аудит будет подключён следующим шагом."
    )


class AuditFlow(StatesGroup):
    waiting_url = State()
    collecting_paste = State()
    collecting_media = State()
    collecting_video_description = State()
    collecting_screenshots = State()
    auditing = State()
    choosing_product = State()
    supplementing_export = State()


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await _do_start(message, state, message.from_user.id)


async def _do_start(message: Message, state: FSMContext, user_id: int) -> None:
    logger.info("User %d started the bot", user_id)

    footer_parts = []
    if settings.SUPPORT_CHANNEL:
        ch = settings.SUPPORT_CHANNEL.lstrip("@")
        footer_parts.append(f'💬 <a href="https://t.me/{ch}">Поддержка</a>')
    if settings.PRIVACY_URL:
        footer_parts.append(f'🔒 <a href="{settings.PRIVACY_URL}">Privacy</a>')
    footer = " | ".join(footer_parts)
    start_footer = f"📖 /help | {footer}" if footer else "📖 /help"

    if _storage_instance is not None and not _storage_instance.is_whitelisted(user_id):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="📩 Запросить доступ",
                    callback_data=f"wl_req:{user_id}"
                )]
            ]
        )
        await message.answer(
            "🔒 Бот в закрытом тестировании.\n\n"
            f"Твой Telegram ID: <code>{user_id}</code>\n\n"
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
    start_caption = _build_start_caption(start_footer)
    start_kb = _build_start_keyboard()
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
        "<b>📊 Экспорт XLSX/CSV — запасной способ</b>\n\n"
        "Основной путь сейчас проще: открой карточку WB/Ozon в браузере, "
        "нажми <code>Ctrl+A</code>, затем <code>Ctrl+C</code> и вставь текст в бот.\n\n"
        "Экспорт из личного кабинета можно использовать, если копипаст страницы неудобен.\n\n"
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
    await _safe_callback_answer(callback)
    return


@router.callback_query(F.data == "how_ozon")
async def how_ozon_cb(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "<b>🛒 Ozon — используй общий способ</b>\n\n"
        "Для Ozon не нужен отдельный маршрут: открой карточку в браузере, "
        "нажми <code>Ctrl+A</code>, затем <code>Ctrl+C</code> и вставь текст в бот "
        "одним или несколькими сообщениями.\n\n"
        "Если Telegram не принимает длинный текст — отправь <code>.txt</code> файл.\n\n"
        "Быстрый аудит проверит текст карточки без фото/видео. "
        "Медиа можно будет добавить отдельным шагом после текстового отчёта.",
        parse_mode="HTML",
    )
    await _safe_callback_answer(callback)


@router.callback_query(F.data == "how_screenshots")
async def how_screenshots_cb(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "<b>📸 Скриншоты — запасной способ</b>\n\n"
        "Основной путь — копипаст текста страницы через <code>Ctrl+A</code> и <code>Ctrl+C</code>.\n\n"
        "Скриншоты сейчас можно использовать как старый запасной ввод. "
        "Отдельный медиа-этап для фото, инфографики, видео и порядка галереи "
        "будет подключено следующим шагом.\n\n"
        "<i>На компьютере: Win+Shift+S → выдели область → Ctrl+V в чат</i>\n"
        "<i>На телефоне: громкость↓ + питание одновременно</i>",
        parse_mode="HTML",
    )
    await _safe_callback_answer(callback)


@router.callback_query(F.data == "how_help")
async def how_help_cb(callback: CallbackQuery) -> None:
    await callback.message.answer(
        _build_help_text(),
        parse_mode="HTML",
        reply_markup=_build_help_keyboard(),
    )
    await _safe_callback_answer(callback)


@router.callback_query(F.data == "back_to_start")
async def back_to_start_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _do_start(callback.message, state, callback.from_user.id)
    await _safe_callback_answer(callback)


@router.callback_query(F.data == "start_guided")
async def start_guided_audit_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await start_guided_audit(callback.message, state)
    await _safe_callback_answer(callback)


@router.callback_query(F.data == "paste_start")
async def paste_start_cb(callback: CallbackQuery, state: FSMContext) -> None:
    limit_msg = _check_audit_limit(callback.from_user.id)
    if limit_msg:
        await callback.message.answer(limit_msg, parse_mode="HTML")
        await _safe_callback_answer(callback)
        return

    await state.clear()
    await state.set_state(AuditFlow.collecting_paste)
    await state.update_data(
        paste_text="",
        paste_parts=0,
        paste_truncated=False,
        paste_source_type="paste",
    )
    await callback.message.answer(
        _paste_instruction_text(),
        reply_markup=_paste_keyboard(),
        parse_mode="HTML",
    )
    await _safe_callback_answer(callback)


@router.callback_query(F.data == "paste_reset")
async def paste_reset_cb(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() != AuditFlow.collecting_paste.state:
        await _safe_callback_answer(callback, "Сначала начни вставку текста карточки", show_alert=True)
        return

    await state.set_state(AuditFlow.collecting_paste)
    await state.update_data(
        paste_text="",
        paste_parts=0,
        paste_truncated=False,
        paste_source_type="paste",
    )
    await callback.message.answer(
        _paste_instruction_text(),
        reply_markup=_paste_keyboard(),
        parse_mode="HTML",
    )
    await _safe_callback_answer(callback)


@router.callback_query(F.data == "paste_run")
async def paste_run_cb(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() != AuditFlow.collecting_paste.state:
        await _safe_callback_answer(callback, "Сначала начни вставку текста карточки", show_alert=True)
        return

    data = await state.get_data()
    paste_text = str(data.get("paste_text", ""))
    if not is_enough_for_quick_audit(paste_text):
        await _safe_callback_answer(callback, "Недостаточно текста карточки", show_alert=True)
        return

    limit_msg = _check_audit_limit(callback.from_user.id)
    if limit_msg:
        await callback.message.answer(limit_msg, parse_mode="HTML")
        await _safe_callback_answer(callback)
        return

    source_type = "txt_file" if data.get("paste_source_type") == "txt_file" else "paste"
    snapshot = parse_marketplace_paste(paste_text, source_type)
    facts = build_local_audit_facts(snapshot)
    await state.update_data(
        paste_snapshot_dump=snapshot.model_dump(),
        paste_facts_dump=facts.model_dump(),
    )
    if snapshot.platform == "unknown" or not snapshot.product_name:
        await callback.message.answer(
            "⚠️ <b>Не удалось определить карточку WB/Ozon.</b>\n\n"
            "AI не запускаю, чтобы не выдумывать данные. Проверь, что в тексте есть "
            "полный копипаст страницы карточки с названием товара и маркерами WB/Ozon, "
            "или отправь .txt файл с копипастом.",
            reply_markup=_paste_keyboard(),
            parse_mode="HTML",
        )
        await _safe_callback_answer(callback)
        return

    await state.set_state(AuditFlow.auditing)
    thinking_msg = await callback.message.answer(
        "🔍 Запускаю быстрый AI-аудит по тексту карточки...\n\n"
        "⏳ Обычно это занимает до 1 минуты.\n"
        "⚠️ <b>Лимит уже проверен, списание будет только после успешного отчёта.</b>",
        parse_mode="HTML",
    )
    await _safe_callback_answer(callback)
    animation_task = asyncio.create_task(_animate_thinking(thinking_msg))

    try:
        report = await run_quick_text_audit(snapshot, facts)
        if not report.platform:
            report.platform = snapshot.platform
        if not report.product_name:
            report.product_name = snapshot.product_name or ""
        if not report.url:
            report.url = "manual_input"
    except QuickAuditError as e:
        logger.warning("Quick paste audit failed: %s", e)
        animation_task.cancel()
        try:
            await thinking_msg.delete()
        except Exception:
            pass
        await state.set_state(AuditFlow.collecting_paste)
        await callback.message.answer(
            "⚠️ <b>Быстрый AI-аудит сейчас не сработал.</b>\n\n"
            "Текст карточки сохранён, бесплатный аудит не списан. "
            "Можно нажать «✅ Запустить быстрый аудит» ещё раз или добавить текст карточки.",
            reply_markup=_paste_keyboard(),
            parse_mode="HTML",
        )
        return

    animation_task.cancel()
    try:
        await thinking_msg.delete()
    except Exception:
        pass

    await send_audit_report(
        callback.message,
        report,
        state=state,
        audit_user_id=callback.from_user.id,
    )
    _log_audit(
        callback.from_user.id,
        callback.from_user.username,
        report.url or "manual_input",
        report.platform or snapshot.platform,
        report.overall_score,
    )
    await state.set_state(AuditFlow.waiting_url)


@router.message(AuditFlow.collecting_paste, F.text)
async def paste_text_received(message: Message, state: FSMContext) -> None:
    text = message.text or ""
    total_chars, parts_count, truncated = await _append_paste_to_state(
        state,
        text,
        "paste",
    )
    await message.answer(
        build_paste_status_text(total_chars, parts_count, truncated),
        reply_markup=_paste_keyboard(),
    )


@router.message(AuditFlow.collecting_paste, F.document)
async def paste_txt_received(message: Message, state: FSMContext, bot: Bot) -> None:
    doc = message.document
    if doc is None:
        return

    if not is_txt_document(doc.file_name):
        await message.answer(
            "В этом режиме нужен .txt или текст сообщением",
            reply_markup=_paste_keyboard(),
        )
        return

    if doc.file_size is not None and doc.file_size > PASTE_TXT_MAX_BYTES:
        await message.answer(
            "⚠️ .txt файл слишком большой. Максимум 128 КБ.",
            reply_markup=_paste_keyboard(),
        )
        return

    try:
        file = await bot.get_file(doc.file_id)
        file_bytes = await bot.download_file(file.file_path)
        data = file_bytes.read(PASTE_TXT_MAX_BYTES + 1)
        if len(data) > PASTE_TXT_MAX_BYTES:
            await message.answer(
                "⚠️ .txt файл слишком большой. Максимум 128 КБ.",
                reply_markup=_paste_keyboard(),
            )
            return
        text = decode_txt_file(data)
    except Exception as e:
        logger.warning(f"TXT paste download failed: {e}")
        await message.answer(
            "⚠️ Не удалось прочитать .txt файл. Попробуй отправить текст сообщением.",
            reply_markup=_paste_keyboard(),
        )
        return

    total_chars, parts_count, truncated = await _append_paste_to_state(
        state,
        text,
        "txt_file",
    )
    await message.answer(
        build_paste_status_text(total_chars, parts_count, truncated),
        reply_markup=_paste_keyboard(),
    )


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
            await send_audit_report(
                message,
                result,
                state=state,
                audit_user_id=message.from_user.id,
            )
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
        await _safe_callback_answer(callback,"⚠️ Недостаточно данных. Отправь текст.", show_alert=True)
        return
    await callback.message.answer("📊 Запускаю аудит...")
    await _run_full_audit(callback.message, state, accumulated, user_id=callback.from_user.id)
    await _safe_callback_answer(callback)


@router.callback_query(F.data == "guide_reset")
async def guide_reset_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AuditFlow.waiting_url)
    await callback.message.answer("🔄 Начинаем заново. Отправь текст карточки или выбери способ.")
    await _safe_callback_answer(callback)


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
            await send_audit_report(
                message,
                result,
                state=state,
                audit_user_id=message.from_user.id,
            )
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

    data = await state.get_data()
    platform = data.get("supplement_platform", "manual")
    try:
        result = await _do_audit_text(text, message, uid, platform=platform)
    except Exception as e:
        logger.warning(f"Audit failed: {e}")
        result = None

    animation_task.cancel()
    try:
        await thinking_msg.delete()
    except Exception:
        pass

    if result:
        await send_audit_report(
            message,
            result,
            state=state,
            audit_user_id=uid,
        )
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


async def _do_audit_text(text: str, message: Message, user_id: int = 0, platform: str = "manual") -> AuditReport | None:
    try:
        safe_text = sanitize_personal_data(text)[-8000:]
        report = await audit_card(safe_text, "", platform)
        _log_audit(user_id or message.from_user.id, message.from_user.username, "", platform, report.overall_score)
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


async def send_audit_report(
    message: Message,
    report: AuditReport,
    state: FSMContext | None = None,
    media_added: bool = False,
    audit_user_id: int = 0,
) -> None:
    if state is not None:
        report_state: dict[str, object] = {
            "last_audit_report": report.model_dump(),
            "last_report_media_added": media_added,
            "last_audit_user_id": audit_user_id,
        }
        if not media_added:
            report_state.update(
                media_items=[],
                video_descriptions=[],
                media_photo_count=0,
            )
        await state.update_data(
            **report_state,
        )

    if report.overall_score:
        filled = min(10, max(1, report.overall_score // 10))
        empty = 10 - filled
        stars = "⭐" * filled + "☆" * empty
        await message.answer(
            f"<b>📊 ОБЩАЯ ОЦЕНКА КАРТОЧКИ: {report.overall_score}/100 {stars}</b>",
            parse_mode="HTML",
        )

    sections = {
        "title": "📝 ЗАГОЛОВОК",
        "price_competitors": "💰 ЦЕНА И КОНКУРЕНТЫ",
        "description": "📄 ОПИСАНИЕ",
        "seo": "🔍 SEO",
        "reviews_risks": "💬 ОТЗЫВЫ И РИСКИ",
        "photos": "📸 ФОТО/ВИДЕО",
        "photo_video": "📸 ФОТО/ВИДЕО",
        "media": "📸 ФОТО/ВИДЕО",
        "gallery": "📸 ФОТО/ВИДЕО",
        "video": "📸 ФОТО/ВИДЕО",
        "competitors": "🕵️ КОНКУРЕНТЫ",
    }

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

    full_text = export_audit_report_text(report, media_added=media_added)
    top_actions = extract_top_actions(report, limit=3)
    if top_actions:
        top_lines = ["✅ <b>3 главных действия</b>"]
        top_lines.extend(
            f"{index}. {_escape(action)}"
            for index, action in enumerate(top_actions, start=1)
        )
        await message.answer("\n".join(top_lines), parse_mode="HTML")

    full_chunks = _safe_send(full_text)
    if len(full_chunks) <= REPORT_TEXT_MAX_CHUNKS:
        await message.answer("📄 Полный текст отчёта:")
        for chunk in full_chunks:
            try:
                await message.answer(chunk)
            except Exception as e:
                logger.warning(f"Failed to send full report chunk: {e}")
                break
    else:
        await message.answer("📄 Полный отчёт длинный, прикрепляю его файлом .txt.")

    try:
        report_file = BufferedInputFile(
            full_text.encode("utf-8"),
            filename=build_report_filename(report),
        )
        await message.answer_document(
            report_file,
            caption="💾 Полный отчёт в .txt",
        )
    except Exception as e:
        logger.warning("Failed to send audit txt file: %s", e, exc_info=True)

    summary_text = full_text
    user_id = message.from_user.id if message.from_user else 0
    copy_key = f"{user_id}:{id(summary_text)}"
    if _storage_instance is not None:
        _storage_instance.store_copy_data(copy_key, summary_text)
    await message.answer(
        "📋 Нажми, чтобы скопировать отчёт целиком.",
        reply_markup=_report_actions_keyboard(copy_key, media_added=media_added),
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
    sections = {
        "title": "=== ЗАГОЛОВОК ===",
        "price_competitors": "=== ЦЕНА И КОНКУРЕНТЫ ===",
        "description": "=== ОПИСАНИЕ ===",
        "seo": "=== SEO ===",
        "reviews_risks": "=== ОТЗЫВЫ И РИСКИ ===",
        "photos": "=== ФОТО/ВИДЕО ===",
        "competitors": "=== КОНКУРЕНТЫ ===",
    }
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
        await _safe_callback_answer(callback,"⚠️ Ошибка хранилища")
        return
    user_id_str = callback.data.split(":", 1)[1]
    if not user_id_str.isdigit():
        await _safe_callback_answer(callback,"⚠️ Некорректный ID")
        return
    user_id = int(user_id_str)
    username = callback.from_user.username or ""
    full_name = callback.from_user.full_name or ""
    encoded = base64.b64encode(username.encode()).decode()
    encoded_fn = base64.b64encode(full_name.encode()).decode()
    admin_id = settings.ADMIN_USER_ID
    if not admin_id:
        await _safe_callback_answer(callback,"⚠️ Администратор не настроен")
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
        safe_full_name = _escape(full_name)
        safe_username = _escape(username)
        await callback.bot.send_message(
            admin_id,
            f"📩 <b>Запрос доступа</b>\n\n👤 {safe_full_name}\n🆔 <code>{user_id}</code>\n{'📛 @' + safe_username if username else '📛 username скрыт'}",
            reply_markup=admin_kb,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Failed to notify admin: {e}")
        await _safe_callback_answer(callback,"⚠️ Не удалось отправить запрос")
        return
    await callback.message.edit_text(
        f"🔒 Бот в закрытом тестировании.\n\n✅ Запрос отправлен. Ожидай одобрения.\n\nТвой ID: <code>{user_id}</code>",
        parse_mode="HTML",
    )
    await _safe_callback_answer(callback,"✅ Запрос отправлен")


@router.callback_query(F.data.startswith("wl_approve:"))
async def approve_access(callback: CallbackQuery) -> None:
    if _storage_instance is None or settings.ADMIN_USER_ID != callback.from_user.id:
        await _safe_callback_answer(callback,"⛔ Нет прав")
        return
    parts = callback.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await _safe_callback_answer(callback,"⚠️ Некорректный ID")
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
    await _safe_callback_answer(callback,"✅ Доступ открыт")


@router.callback_query(F.data.startswith("wl_reject:"))
async def reject_access(callback: CallbackQuery) -> None:
    if _storage_instance is None or settings.ADMIN_USER_ID != callback.from_user.id:
        await _safe_callback_answer(callback,"⛔ Нет прав")
        return
    user_id_str = callback.data.split(":", 1)[1]
    if not user_id_str.isdigit():
        await _safe_callback_answer(callback,"⚠️ Некорректный ID")
        return
    user_id = int(user_id_str)
    await callback.message.edit_text(callback.message.html_text + "\n\n❌ <b>Отклонено</b>", parse_mode="HTML")
    try:
        await callback.bot.send_message(user_id, "❌ В доступе отказано.")
    except Exception as e:
        logger.warning(f"Failed to notify user {user_id} about rejection: {e}")
    await _safe_callback_answer(callback,"❌ Отклонено")


@router.callback_query(F.data.startswith("copy_audit:"))
async def copy_audit_report(callback: CallbackQuery) -> None:
    if _storage_instance is None:
        await _safe_callback_answer(callback,"⚠️ Отчёт не найден")
        return
    key = callback.data.removeprefix("copy_audit:")
    text = _storage_instance.get_copy_data(key)
    if not text:
        await _safe_callback_answer(callback,"⚠️ Отчёт устарел")
        return
    for chunk in _safe_send_pre_chunks(text):
        await callback.message.answer(f"<pre>{_escape(chunk)}</pre>", parse_mode="HTML")
    await _safe_callback_answer(callback,"📋 Текст отчёта ниже — выдели и скопируй сам")


@router.callback_query(F.data == "media_next_step")
async def media_next_step_cb(callback: CallbackQuery, state: FSMContext) -> None:
    limit_msg = await _check_media_audit_limit(callback.from_user.id, state)
    if limit_msg:
        await callback.message.answer(limit_msg, parse_mode="HTML")
        await _safe_callback_answer(callback)
        return

    data = await state.get_data()
    await state.set_state(AuditFlow.collecting_media)
    await state.update_data(
        media_items=list(data.get("media_items", [])),
        video_descriptions=list(data.get("video_descriptions", [])),
        media_photo_count=int(data.get("media_photo_count", 0)),
    )
    await callback.message.answer(
        _build_media_start_text(),
        reply_markup=_media_keyboard(),
        parse_mode="HTML",
    )
    await _safe_callback_answer(callback)


@router.message(AuditFlow.collecting_media, F.photo)
async def media_photo_received(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.from_user is None:
        return

    data = await state.get_data()
    photo_count = int(data.get("media_photo_count", 0))
    if photo_count >= settings.MEDIA_MAX_PHOTOS:
        await message.answer(
            f"⚠️ Уже принято {settings.MEDIA_MAX_PHOTOS} фото — это максимум для одного медиа-блока.",
            reply_markup=_media_keyboard(),
        )
        return

    limit_msg = await _check_media_audit_limit(message.from_user.id, state)
    if limit_msg:
        await message.answer(limit_msg, parse_mode="HTML")
        return

    photo = message.photo[-1]
    if photo.file_size and photo.file_size > settings.MEDIA_MAX_IMAGE_BYTES:
        await message.answer(
            "⚠️ Фото слишком большое. Отправь изображение до "
            f"{settings.MEDIA_MAX_IMAGE_BYTES // (1024 * 1024)} МБ.",
            reply_markup=_media_keyboard(),
        )
        return

    position = photo_count + 1
    status_message = await message.answer(f"📸 Фото {position} принято, анализирую...")
    try:
        file = await bot.get_file(photo.file_id)
        downloaded = await bot.download_file(file.file_path)
        image_data = downloaded.read()
        if len(image_data) > settings.MEDIA_MAX_IMAGE_BYTES:
            await status_message.answer(
                "⚠️ Фото слишком большое. Отправь изображение до "
                f"{settings.MEDIA_MAX_IMAGE_BYTES // (1024 * 1024)} МБ.",
                reply_markup=_media_keyboard(),
            )
            return
        media_item = await call_gemini_media_audit(image_data, position)
    except MediaAuditError as exc:
        logger.warning("Media audit failed for photo %d: %s", position, exc.__class__.__name__)
        await status_message.answer(
            f"⚠️ Не удалось разобрать фото {position}: {_escape(str(exc))}\n\n"
            "Можно отправить другое фото или нажать «✅ Я отправил все фото».",
            reply_markup=_media_keyboard(),
            parse_mode="HTML",
        )
        return
    except Exception as exc:
        logger.warning("Unexpected media audit failure for photo %d: %s", position, exc.__class__.__name__)
        await status_message.answer(
            f"⚠️ Не удалось разобрать фото {position}. Можно попробовать ещё раз.",
            reply_markup=_media_keyboard(),
        )
        return

    data = await state.get_data()
    media_items = list(data.get("media_items", []))
    media_items.append(media_item.model_dump())
    await state.update_data(
        media_items=media_items,
        media_photo_count=position,
    )

    await status_message.answer(
        "✅ <b>Фото принято</b>\n\n"
        f"Позиция: {media_item.position}\n"
        f"Тип: {_escape(_media_type_label(media_item.media_type))}\n"
        f"Предварительный вердикт: {_escape(_media_verdict_label(media_item.preliminary_verdict))}\n\n"
        "Можно отправить следующее фото или нажать «✅ Я отправил все фото».",
        reply_markup=_media_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "media_describe_video")
async def media_describe_video_cb(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    videos = list(data.get("video_descriptions", []))
    if len(videos) >= settings.MEDIA_MAX_VIDEOS:
        await _safe_callback_answer(
            callback,
            f"Максимум {settings.MEDIA_MAX_VIDEOS} описания видео",
            show_alert=True,
        )
        return

    await state.set_state(AuditFlow.collecting_video_description)
    await callback.message.answer(
        build_video_description_help_text(),
        reply_markup=_media_video_keyboard(),
        parse_mode="HTML",
    )
    await _safe_callback_answer(callback)


@router.callback_query(F.data == "media_back_to_photos")
async def media_back_to_photos_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AuditFlow.collecting_media)
    await callback.message.answer(
        "📸 Вернулись к фото. Можно отправить следующее фото или завершить медиа-блок.",
        reply_markup=_media_keyboard(),
    )
    await _safe_callback_answer(callback)


@router.message(AuditFlow.collecting_video_description, F.text)
async def media_video_description_received(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    videos = list(data.get("video_descriptions", []))
    if len(videos) >= settings.MEDIA_MAX_VIDEOS:
        await state.set_state(AuditFlow.collecting_media)
        await message.answer(
            f"⚠️ Уже принято {settings.MEDIA_MAX_VIDEOS} описания видео — это максимум.",
            reply_markup=_media_keyboard(),
        )
        return

    try:
        video = parse_video_description(message.text or "")
    except MediaAuditError as exc:
        await message.answer(
            f"⚠️ {_escape(str(exc))}\n\n"
            "Заполни шаблон ещё раз или вернись к фото.",
            reply_markup=_media_video_keyboard(),
            parse_mode="HTML",
        )
        return

    videos.append(video.model_dump())
    await state.update_data(video_descriptions=videos)
    await state.set_state(AuditFlow.collecting_media)
    await message.answer(
        "✅ <b>Описание видео принято</b>\n\n"
        f"Тип: {_escape(_video_type_label(video.video_type))}\n"
        f"Позиция: {_escape(str(video.position)) if video.position is not None else 'не указана'}\n\n"
        "Можно отправить фото, описать ещё одно видео или завершить медиа-блок.",
        reply_markup=_media_keyboard(),
        parse_mode="HTML",
    )


@router.message(AuditFlow.collecting_media, F.video)
@router.message(AuditFlow.collecting_media, F.animation)
@router.message(AuditFlow.collecting_media, F.document)
async def media_file_rejected(message: Message) -> None:
    await message.answer(
        "⚠️ В MVP видео и файлы не загружаются для медиа-аудита.\n\n"
        "Отправляй фото галереи как изображения, а видео опиши текстом по кнопке «🎥 Описать видео».",
        reply_markup=_media_keyboard(),
    )


@router.message(AuditFlow.collecting_media, F.text)
async def media_text_received_in_photo_flow(message: Message) -> None:
    await message.answer(
        "📸 Сейчас жду фото галереи. Если это описание видео, нажми «🎥 Описать видео» и заполни шаблон.",
        reply_markup=_media_keyboard(),
    )


@router.callback_query(F.data == "media_photos_done")
async def media_photos_done_cb(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    media_items = [
        MediaItem.model_validate(item)
        for item in list(data.get("media_items", []))
    ]
    videos = [
        VideoDescription.model_validate(item)
        for item in list(data.get("video_descriptions", []))
    ]
    if not media_items and not videos:
        await _safe_callback_answer(
            callback,
            "Добавьте хотя бы одно фото или описание видео",
            show_alert=True,
        )
        return

    audit_items = build_media_audit_items(media_items, videos)
    saved_report = data.get("last_audit_report")
    if isinstance(saved_report, dict):
        report = AuditReport.model_validate(saved_report)
        report.items.extend(audit_items)
        report.summary = (
            f"{report.summary}\n\nМедиа-блок добавлен: проверено фото — "
            f"{len(media_items)}, описаний видео — {len(videos)}."
        ).strip()
    else:
        snapshot_dump = data.get("paste_snapshot_dump")
        product_name = ""
        platform = ""
        if isinstance(snapshot_dump, dict):
            product_name = str(snapshot_dump.get("product_name") or "")
            platform = str(snapshot_dump.get("platform") or "")
        report = AuditReport(
            url="manual_input",
            platform=platform,
            product_name=product_name,
            overall_score=0,
            items=audit_items,
            summary=(
                "Медиа-блок собран отдельно: быстрый текстовый аудит повторно не запускался. "
                f"Проверено фото — {len(media_items)}, описаний видео — {len(videos)}."
            ),
        )

    await _safe_callback_answer(callback)
    await callback.message.answer("✅ Собираю обновлённый отчёт с медиа-блоком...")
    await send_audit_report(
        callback.message,
        report,
        state=state,
        media_added=True,
        audit_user_id=callback.from_user.id,
    )
    await state.set_state(AuditFlow.waiting_url)


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
        await _safe_callback_answer(callback,"⚠️ Некорректный выбор")
        return

    row_num = int(row_str)
    products = _export_cache.get(callback.from_user.id, [])
    product = None
    for p in products:
        if p.row == row_num:
            product = p
            break

    if not product:
        await _safe_callback_answer(callback,"⚠️ Товар не найден. Загрузите файл заново.")
        return

    limit_msg = _check_audit_limit(callback.from_user.id)
    if limit_msg:
        await callback.message.answer(limit_msg, parse_mode="HTML")
        await state.set_state(AuditFlow.waiting_url)
        await _safe_callback_answer(callback)
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

    present: list[str] = [f"• Название: {_escape(product.title[:60])}"]
    if product.brand:
        present.append(f"• Бренд: {_escape(product.brand)}")
    if product.price:
        present.append(f"• Цена: {_escape(product.price)} ₽")
    if product.description:
        present.append(f"• Описание: есть ({len(product.description)} симв.)")
    if product.category:
        present.append(f"• Категория: {_escape(product.category)}")

    missing_text = "\n".join(f"  {m}" for m in missing)
    present_text = "\n".join(present)
    sent = await callback.message.answer(
        f"✅ <b>{_escape(product.title[:80])}</b>\n\n"
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
    await _safe_callback_answer(callback)


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
        if not ocr_text.strip().startswith("НОМЕР:"):
            ocr_text = f"[ФОТО {pcount}]\n{ocr_text.strip()}"
        data = await state.get_data()
        accumulated = data.get("accumulated_text", "")
        accumulated = accumulated + "\n---\n" + ocr_text.strip()
        if len(accumulated) > 8000:
            accumulated = accumulated[-8000:]
        await state.update_data(accumulated_text=accumulated)
        logger.info(f"Photo {pcount}: OCR OK ({len(ocr_text)} chars)")

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
        await _safe_callback_answer(callback,"⚠️ Недостаточно данных. Отправьте описание или скриншоты.", show_alert=True)
        return
    await callback.message.answer("📊 Запускаю аудит...")
    await _safe_callback_answer(callback)
    await _run_full_audit(callback.message, state, accumulated, user_id=callback.from_user.id)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        _build_help_text(),
        parse_mode="HTML",
        reply_markup=_build_help_keyboard(),
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
            display = _escape(full_name)
        elif username and username != "admin":
            display = f"@{_escape(username)}"
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
