"""Cleaner for Wildberries copy-paste dumps.

Removes navigation, footers, duplicate blocks, and boilerplate.
"""

import re

# Patterns that indicate junk sections
_JUNK_STARTS = [
    "Лови лучшие цены",
    "Открыть",
    "Найти на Wildberries",
    "РАСПРОДАЖА",
    "Скидки WB Клуба",
    "Сертификаты Wildberries",
    "Покупайте как бизнес",
    "Лотерея Мечталлион",
    "Мужчины выбирают",
    "Товары для взрослых",
    "Сделано в России",
    "Товары оптом",
    "Грузовая доставка",
    "Культурный код",
    "Баллы за отзыв",
    "Акции",
    "Цифровые товары",
    "Покупателям",
    "Продавцам и партнёрам",
    "Наши проекты",
    "Компания",
    "AppStore",
    "Google Play",
    "AppGallery",
    "RuStore",
    "Avrora",
    "© Wildberries",
    "Вы недавно смотрели",
    "См. все",
    "Смотрите также",
    "Продавец рекомендует",
    "Похожие",
    "У других продавцов",
    "Нет в наличии",
    "Войдите, чтобы задать вопрос",
    "Хотите что-то узнать о товаре",
    "Задать вопрос продавцу",
    "Ответ полезен?",
    "Текст составила нейросеть Wildberries",
    "Первоначальный отзыв",
    "По закону этот товар",
    "Продавец сообщил",
    "Вся информация предоставлена продавцом",
]

_JUNK_CONTAINS = [
    "кешбэк",
    "с WB Кошельком",
    "Выкупили",
]

# Category names that appear in WB navigation
_CATEGORIES = [
    "Женщинам", "Обувь", "Детям", "Мужчинам", "Дом", "Красота",
    "Аксессуары", "Электроника", "Игрушки", "Мебель", "Продукты",
    "Цветы", "Бытовая техника", "Зоотовары", "Спорт", "Автотовары",
    "Транспортные средства", "Книги", "Ювелирные изделия",
    "Для ремонта", "Сад и дача", "Здоровье", "Адаптивные товары",
    "Лекарственные препараты", "Канцтовары", "Еаптека",
    "РИВ ГОШ", "Ресейл", "Бренды", "Travel", "Wibes",
    "Новостройки", "Экспресс", "RWB Участие",
]


def clean_wb_text(text: str) -> str:
    """Remove navigation, footers, duplicates from WB copy-paste dump."""
    lines = text.split("\n")
    cleaned: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned.append("")
            continue

        # Skip navigation category lines
        if stripped in _CATEGORIES:
            continue

        # Skip lines starting with junk patterns
        skip = False
        for junk in _JUNK_STARTS:
            if stripped.startswith(junk):
                skip = True
                break

        # Skip lines containing junk patterns
        if not skip:
            for junk in _JUNK_CONTAINS:
                if junk in stripped.lower():
                    skip = True
                    break

        if not skip:
            cleaned.append(stripped)

    result = "\n".join(cleaned)

    # Remove duplicate blocks (same text appearing multiple times)
    result = _remove_duplicate_blocks(result)

    # Remove "Ответ продавца" boilerplate
    result = _remove_seller_replies(result)

    # Remove "Плюсы товара" blocks
    result = _remove_plusy_tovara(result)

    # Collapse multiple empty lines
    result = re.sub(r"\n{3,}", "\n\n", result)

    return result.strip()


def _remove_duplicate_blocks(text: str) -> str:
    """Remove large duplicate sections (the same card info repeated)."""
    paragraphs = text.split("\n\n")
    seen: set[str] = set()
    unique: list[str] = []

    for para in paragraphs:
        normalized = para.strip()[:200]
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(para)
        elif not normalized:
            unique.append(para)

    return "\n\n".join(unique)


def _remove_seller_replies(text: str) -> str:
    """Remove 'Ответ продавца' blocks — they're boilerplate upselling."""
    pattern = r"Ответ продавца[\s\S]*?(?=(?:Аватар пользователя|Хотите что|$))"
    return re.sub(pattern, "", text)


def _remove_plusy_tovara(text: str) -> str:
    """Remove 'Плюсы товара' metadata blocks."""
    pattern = r"Плюсы товара[\s\S]*?(?=\n\n|\nАватар|$)"
    return re.sub(pattern, "", text)
