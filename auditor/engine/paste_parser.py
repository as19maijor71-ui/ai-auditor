from __future__ import annotations

import re
from collections import Counter
from typing import Literal

from auditor.engine.paste_models import (
    CompetitorCard,
    LocalAuditFacts,
    MarketplaceCardSnapshot,
)

Platform = Literal["wb", "ozon", "unknown"]
SourceType = Literal["paste", "txt_file"]
MAX_PASTE_RAW_CHARS = 30_000
MAX_TXT_FILE_RAW_CHARS = 128 * 1024

_PRICE_RE = re.compile(
    r"(?<![\d,.])(\d{1,3}(?:[\s\u00a0]\d{3})+|\d{3,7})(?:[,.]\d{2})?\s*(?:₽|руб\.?|р\b)",
    re.IGNORECASE,
)
_COUNT_NUMBER_RE = r"(\d{1,3}(?:[\s\u00a0]\d{3})*|\d+)"
_RATING_RE = re.compile(r"(?<!\d)([1-5][,.][0-9])(?:\s*(?:из\s*5|/5))?(?!\d)", re.IGNORECASE)

_COMPETITOR_HEADINGS = {
    "смотрите также",
    "рекомендуем также",
    "подобрали для вас",
    "похожие товары",
    "вам может понравиться",
    "покупают вместе",
    "с этим товаром покупают",
}

_SECTION_HEADINGS = {
    "о товаре",
    "описание",
    "описание товара",
    "характеристики",
    "основные характеристики",
    "комплектация",
    "состав",
    "отзывы",
    "отзывы покупателей",
    "вопросы",
    "вопросы о товаре",
}

_DESCRIPTION_HEADINGS = {"описание", "описание товара"}
_CHARACTERISTICS_HEADINGS = {"характеристики", "основные характеристики", "о товаре"}
_REVIEW_HEADINGS = {"отзывы", "отзывы покупателей"}

_JUNK_EXACT = {
    "каталог",
    "корзина",
    "избранное",
    "профиль",
    "войти",
    "поиск",
    "найти",
    "меню",
    "главная",
    "доставка",
    "оплата",
    "возврат",
    "контакты",
    "помощь",
    "личный кабинет",
    "appstore",
    "google play",
    "appgallery",
    "rustore",
    "для слабовидящих",
    "в сравнение",
    "поделиться",
    "купить сейчас",
    "добавить в корзину",
    "в корзину",
}

_JUNK_PREFIXES = (
    "покупателям",
    "продавцам",
    "продавцам и партнёрам",
    "наши проекты",
    "компания",
    "об ozon",
    "об озон",
    "ozon беларусь",
    "ozon казахстан",
    "ozon узбекистан",
    "wildberries 6+",
    "найти на wildberries",
    "искать на ozon",
    "наведите камеру",
    "скачайте приложение",
    "открыть приложение",
    "пункт выдачи",
    "выберите адрес",
    "город доставки",
    "адрес доставки",
    "мой адрес",
    "телефон получателя",
    "мой телефон",
    "cookie",
    "cookies",
)

_JUNK_CONTAINS = (
    "политика обработки",
    "политика конфиденциальности",
    "пользовательское соглашение",
    "применяются рекомендательные технологии",
    "мы используем cookie",
    "мы используем cookies",
    "все права защищены",
    "©",
)

_NON_CHARACTERISTIC_KEYS = {
    "название",
    "название товара",
    "наименование товара",
    "товар",
    "бренд",
    "цена",
    "текущая цена",
    "старая цена",
    "цена до скидки",
    "рейтинг",
    "отзывы",
    "оценки",
    "вопросы",
    "продавец",
    "магазин",
    "артикул",
    "артикул wb",
    "ozon id",
    "категория",
    "варианты",
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)"
)
_ADDRESS_RE = re.compile(
    r"\b(?:ул\.|улица|проспект|пр-т|переулок|пер\.|шоссе|дом\s+\d|д\.\s*\d|кв\.|квартира)\b",
    re.IGNORECASE,
)
_PII_LABEL_RE = re.compile(
    r"^(?:адрес|адрес доставки|город доставки|выберите адрес|пункт выдачи|мой адрес|телефон|телефон получателя|мой телефон|e-?mail|почта|получател(?:ь|ю|я)|фио|имя получателя)\s*[:—-]",
    re.IGNORECASE,
)


def normalize_paste_text(raw_text: str) -> str:
    text = (
        raw_text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u00a0", " ")
        .replace("\t", " ")
    )
    lines: list[str] = []
    previous_blank = False

    for raw_line in text.split("\n"):
        line = re.sub(r" {2,}", " ", raw_line.strip())
        if not line:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue

        previous_blank = False
        if _is_junk_line(line) or _contains_personal_data(line):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)

    while lines and not lines[-1]:
        lines.pop()

    return "\n".join(lines).strip()


def sanitize_personal_data(text: str) -> str:
    safe_lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if _contains_personal_data(line):
            continue
        safe_lines.append(raw_line)
    return "\n".join(safe_lines)


def detect_paste_platform(text: str) -> Platform:
    normalized = text.lower()
    wb_score = 0
    ozon_score = 0

    wb_patterns = (
        r"\bwildberries\b",
        r"\bwb\b",
        r"вайлдберриз",
        r"артикул\s*wb",
        r"wb\s*кошел",
        r"wb\s*клуб",
        r"wildberries\.ru",
    )
    ozon_patterns = (
        r"\bozon\b",
        r"\bозон\b",
        r"ozon\s*card",
        r"ozon\s*кар",
        r"ozon\s*id",
        r"продавец\s*ozon",
        r"ozon\.ru",
    )

    for pattern in wb_patterns:
        if re.search(pattern, normalized, re.IGNORECASE):
            wb_score += 1
    for pattern in ozon_patterns:
        if re.search(pattern, normalized, re.IGNORECASE):
            ozon_score += 1

    if wb_score > ozon_score and wb_score > 0:
        return "wb"
    if ozon_score > wb_score and ozon_score > 0:
        return "ozon"
    return "unknown"


def parse_marketplace_paste(
    raw_text: str,
    source_type: SourceType = "paste",
) -> MarketplaceCardSnapshot:
    safe_source_type = _normalize_source_type(source_type)
    limited_raw_text, input_truncated = _limit_raw_text(raw_text, safe_source_type)
    safe_raw_text = sanitize_personal_data(limited_raw_text)
    cleaned_text = normalize_paste_text(safe_raw_text)
    platform = detect_paste_platform(cleaned_text)
    lines = cleaned_text.splitlines()
    main_lines, competitor_lines = _split_main_and_competitor_lines(lines)
    main_text = "\n".join(main_lines)

    product_name = _extract_product_name(main_lines, allow_unlabeled=platform != "unknown")
    competitor_cards = _extract_competitor_cards(competitor_lines)
    description = _extract_description(main_lines)
    characteristics = _extract_characteristics(main_lines)
    current_price = _extract_current_price(main_lines)
    old_price = _extract_old_price(main_lines, current_price)

    snapshot = MarketplaceCardSnapshot(
        platform=platform,
        source_type=safe_source_type,
        raw_text=safe_raw_text,
        cleaned_text=cleaned_text,
        product_name=product_name,
        brand=_extract_labeled_value(main_lines, ("Бренд", "Brand")),
        sku=_extract_sku(main_text),
        category_path=_extract_category_path(main_lines),
        current_price=current_price,
        old_price=old_price,
        unit_price=_extract_unit_price(main_text),
        rating=_extract_rating(main_text),
        review_count=_extract_review_count(main_text),
        question_count=_extract_question_count(main_text),
        seller_name=_extract_seller_name(main_lines),
        variants=_extract_variants(main_lines),
        characteristics=characteristics,
        description=description,
        review_fragments=_extract_review_fragments(main_lines),
        competitor_cards=competitor_cards,
        missing_blocks=[],
    )
    snapshot.missing_blocks = _build_missing_blocks(snapshot)
    if input_truncated:
        snapshot.missing_blocks.append("input_truncated")
    return snapshot


def build_local_audit_facts(snapshot: MarketplaceCardSnapshot) -> LocalAuditFacts:
    title = snapshot.product_name or ""
    title_length = len(title) if snapshot.product_name is not None else None
    words = re.findall(r"[a-zа-яё0-9]+", title.lower())
    repeated_words = {
        word: count
        for word, count in Counter(word for word in words if len(word) > 2).items()
        if count > 1
    }
    competitor_prices = [
        card.price for card in snapshot.competitor_cards if card.price is not None
    ]

    return LocalAuditFacts(
        title_length=title_length,
        repeated_words=repeated_words,
        has_description=bool(snapshot.description and snapshot.description.strip()),
        description_length=len(snapshot.description or ""),
        characteristics_count=len(snapshot.characteristics),
        has_price=snapshot.current_price is not None,
        has_rating=snapshot.rating is not None,
        has_reviews=snapshot.review_count is not None and snapshot.review_count > 0,
        competitors_count=len(snapshot.competitor_cards),
        min_competitor_price=min(competitor_prices) if competitor_prices else None,
        avg_competitor_price=round(sum(competitor_prices) / len(competitor_prices))
        if competitor_prices
        else None,
    )


def _is_junk_line(line: str) -> bool:
    lowered = line.lower()
    if lowered in _JUNK_EXACT:
        return True
    if any(lowered.startswith(prefix) for prefix in _JUNK_PREFIXES):
        return True
    return any(fragment in lowered for fragment in _JUNK_CONTAINS)


def _contains_personal_data(line: str) -> bool:
    if not line:
        return False
    return (
        _EMAIL_RE.search(line) is not None
        or _PHONE_RE.search(line) is not None
        or _ADDRESS_RE.search(line) is not None
        or _PII_LABEL_RE.search(line) is not None
    )


def _limit_raw_text(raw_text: str, source_type: SourceType) -> tuple[str, bool]:
    max_chars = MAX_TXT_FILE_RAW_CHARS if source_type == "txt_file" else MAX_PASTE_RAW_CHARS
    if len(raw_text) <= max_chars:
        return raw_text, False
    return raw_text[:max_chars], True


def _split_main_and_competitor_lines(lines: list[str]) -> tuple[list[str], list[str]]:
    main_lines: list[str] = []
    competitor_lines: list[str] = []
    in_competitor_block = False

    for line in lines:
        if _is_competitor_heading(line):
            in_competitor_block = True
            continue

        if in_competitor_block and _is_main_section_heading(line):
            in_competitor_block = False

        if in_competitor_block:
            competitor_lines.append(line)
            continue

        main_lines.append(line)

    return main_lines, competitor_lines


def _is_competitor_heading(line: str) -> bool:
    lowered = line.strip().lower().strip(":")
    return lowered in _COMPETITOR_HEADINGS


def _is_main_section_heading(line: str) -> bool:
    lowered = line.strip().lower().strip(":")
    return lowered in _SECTION_HEADINGS


def _normalize_source_type(source_type: str) -> SourceType:
    if source_type in ("paste", "txt_file"):
        return source_type
    return "paste"


def _extract_product_name(lines: list[str], allow_unlabeled: bool) -> str | None:
    labeled = _extract_labeled_value(
        lines,
        ("Название товара", "Название", "Наименование товара", "Товар"),
    )
    if labeled:
        return labeled

    if allow_unlabeled:
        for line in lines:
            if _is_title_candidate(line):
                return _clean_name(line)
    return None


def _extract_labeled_value(lines: list[str], labels: tuple[str, ...]) -> str | None:
    labels_lower = tuple(label.lower() for label in labels)
    for index, line in enumerate(lines):
        for label, label_lower in zip(labels, labels_lower):
            match = re.match(
                rf"^{re.escape(label)}\s*[:—-]\s*(.+)$",
                line,
                re.IGNORECASE,
            )
            if match:
                value = match.group(1).strip()
                return value or None
            if line.strip().lower() == label_lower:
                return _next_content_line(lines, index + 1)
    return None


def _next_content_line(lines: list[str], start_index: int) -> str | None:
    for line in lines[start_index:]:
        stripped = line.strip()
        if stripped and not _is_junk_line(stripped):
            return stripped
    return None


def _clean_name(line: str) -> str:
    name = re.sub(r"^\d+[\).\s-]+", "", line).strip()
    name = re.sub(r"\s+", " ", name)
    return name.strip(" -—")


def _is_title_candidate(line: str) -> bool:
    stripped = _clean_name(line)
    lowered = stripped.lower().strip(":")
    if not (6 <= len(stripped) <= 220):
        return False
    if not re.search(r"[A-Za-zА-Яа-яЁё]", stripped):
        return False
    if lowered in _SECTION_HEADINGS or _is_competitor_heading(stripped):
        return False
    if any(marker in lowered for marker in ("₽", "отзыв", "оцен", "вопрос", "рейтинг")):
        return False
    if re.search(r"\d[,.]\d", stripped) and "модель" not in lowered:
        return False
    if ":" in stripped:
        return False
    if "/" in stripped or ">" in stripped:
        return False
    if lowered in {"wildberries", "ozon", "озон", "вайлдберриз"}:
        return False
    return True


def _extract_sku(text: str) -> str | None:
    patterns = (
        r"(?:артикул\s*wb|артикул\s*ozon|ozon\s*id|код\s*товара|sku)\s*[:№#-]?\s*([A-Za-zА-Яа-я0-9_-]{3,})",
        r"артикул\s*[:№#-]?\s*([A-Za-zА-Яа-я0-9_-]{3,})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _extract_category_path(lines: list[str]) -> list[str]:
    for line in lines:
        value = None
        match = re.match(r"^Категория\s*[:—-]\s*(.+)$", line, re.IGNORECASE)
        if match:
            value = match.group(1)
        elif _looks_like_category_breadcrumb(line):
            value = line

        if value:
            parts = [
                part.strip()
                for part in re.split(r"\s*(?:/|>|→)\s*", value)
                if part.strip()
            ]
            return [part for part in parts if part.lower() != "главная"]
    return []


def _looks_like_category_breadcrumb(line: str) -> bool:
    lowered = line.lower()
    if not ((" / " in line or " > " in line or " → " in line) and len(line) < 180):
        return False
    if ":" in line or "₽" in line or "руб" in lowered or lowered.startswith("цена"):
        return False
    parts = [
        part.strip()
        for part in re.split(r"\s*(?:/|>|→)\s*", line)
        if part.strip()
    ]
    return len(parts) >= 2


def _extract_current_price(lines: list[str]) -> int | None:
    current_labels = ("текущая цена", "цена с картой", "цена ozon card", "цена", "price")
    old_markers = ("старая", "до скидки", "без скидки", "зачерк")

    for line in lines:
        lowered = line.lower()
        if any(marker in lowered for marker in old_markers):
            continue
        if any(lowered.startswith(label) for label in current_labels):
            price = _parse_price(line)
            if price is not None:
                return price

    for line in lines[:60]:
        lowered = line.lower()
        if any(marker in lowered for marker in old_markers) or "/" in line:
            continue
        price = _parse_price(line)
        if price is not None:
            return price
    return None


def _extract_old_price(lines: list[str], current_price: int | None) -> int | None:
    old_markers = ("старая цена", "цена до скидки", "без скидки", "зачеркнутая")
    for line in lines:
        lowered = line.lower()
        if any(marker in lowered for marker in old_markers):
            price = _parse_price(line)
            if price is not None:
                return price

    prices = [
        price
        for line in lines[:60]
        for price in [_parse_price(line)]
        if price is not None
    ]
    if current_price is not None:
        higher_prices = [price for price in prices if price > current_price]
        return higher_prices[0] if higher_prices else None
    return prices[1] if len(prices) > 1 else None


def _parse_price(text: str) -> int | None:
    match = _PRICE_RE.search(text)
    if not match:
        label_match = re.search(r"[:—-]\s*(\d{2,7})\s*$", text)
        if not label_match:
            return None
        raw_price = label_match.group(1)
    else:
        raw_price = match.group(1)
    digits = re.sub(r"\D", "", raw_price)
    return int(digits) if digits else None


def _extract_unit_price(text: str) -> str | None:
    match = re.search(
        r"\d[\d\s\u00a0]*\s*(?:₽|руб\.?)\s*/\s*(?:100\s*г|100\s*мл|кг|г|л|мл|шт|ед\.?)",
        text,
        re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", match.group(0)).strip() if match else None


def _extract_rating(text: str) -> float | None:
    label_match = re.search(r"рейтинг\s*[:—-]\s*([1-5][,.][0-9])", text, re.IGNORECASE)
    if label_match:
        return _parse_rating_value(label_match.group(1))

    for line in text.splitlines():
        lowered = line.lower()
        if "★" in line or any(marker in lowered for marker in ("рейтинг", "отзыв", "оцен")):
            match = _RATING_RE.search(line)
            if match:
                return _parse_rating_value(match.group(1))
    return None


def _parse_rating_value(raw_value: str) -> float | None:
    try:
        value = float(raw_value.replace(",", "."))
    except ValueError:
        return None
    if 0 < value <= 5:
        return value
    return None


def _extract_review_count(text: str) -> int | None:
    return _extract_count(text, r"отзыв(?:а|ов)?|оцен(?:ка|ки|ок)")


def _extract_question_count(text: str) -> int | None:
    return _extract_count(text, r"вопрос(?:а|ов)?")


def _extract_count(text: str, noun_pattern: str) -> int | None:
    label_match = re.search(
        rf"(?:{noun_pattern})\s*[:—-]\s*{_COUNT_NUMBER_RE}",
        text,
        re.IGNORECASE,
    )
    if label_match:
        return _parse_count(label_match.group(1))

    match = re.search(
        rf"(?<![\d,.]){_COUNT_NUMBER_RE}\s*(?:{noun_pattern})",
        text,
        re.IGNORECASE,
    )
    if match:
        return _parse_count(match.group(1))
    return None


def _parse_count(raw_value: str) -> int:
    return int(re.sub(r"\D", "", raw_value))


def _extract_seller_name(lines: list[str]) -> str | None:
    return _extract_labeled_value(lines, ("Продавец", "Магазин", "Поставщик"))


def _extract_variants(lines: list[str]) -> list[str]:
    value = _extract_labeled_value(lines, ("Варианты", "Другие варианты", "Цвета"))
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,;/]", value) if part.strip()]


def _extract_description(lines: list[str]) -> str | None:
    chunks: list[str] = []
    collecting = False

    for line in lines:
        lowered = line.lower().strip(":")
        if lowered in _DESCRIPTION_HEADINGS:
            collecting = True
            continue
        match = re.match(r"^Описание(?: товара)?\s*[:—-]\s*(.+)$", line, re.IGNORECASE)
        if match:
            collecting = True
            chunks.append(match.group(1).strip())
            continue
        if collecting and (_is_stop_heading(line) or lowered in _CHARACTERISTICS_HEADINGS):
            break
        if collecting and line.strip():
            chunks.append(line.strip())

    description = "\n".join(chunks).strip()
    return description or None


def _extract_characteristics(lines: list[str]) -> dict[str, str]:
    characteristics: dict[str, str] = {}
    collecting = False

    for line in lines:
        lowered = line.lower().strip(":")
        if lowered in _CHARACTERISTICS_HEADINGS:
            collecting = True
            continue
        if collecting and lowered in _DESCRIPTION_HEADINGS | _REVIEW_HEADINGS:
            break
        if collecting and _is_competitor_heading(line):
            break

        pair = _parse_key_value(line)
        if pair and (collecting or _is_likely_characteristic(pair[0])):
            key, value = pair
            characteristics[key] = value

    return characteristics


def _parse_key_value(line: str) -> tuple[str, str] | None:
    match = re.match(r"^([^:—-]{2,45})\s*[:—-]\s*(.{1,160})$", line)
    if not match:
        return None
    key = match.group(1).strip()
    value = match.group(2).strip()
    if not key or not value:
        return None
    if key.lower() in _NON_CHARACTERISTIC_KEYS:
        return None
    return key, value


def _is_likely_characteristic(key: str) -> bool:
    lowered = key.lower()
    return lowered in {
        "тип",
        "материал",
        "состав",
        "цвет",
        "размер",
        "объем",
        "объём",
        "вес",
        "страна производства",
        "страна-изготовитель",
        "модель",
        "назначение",
        "комплектация",
        "срок годности",
    }


def _is_stop_heading(line: str) -> bool:
    lowered = line.lower().strip(":")
    return lowered in _SECTION_HEADINGS or _is_competitor_heading(line)


def _extract_review_fragments(lines: list[str]) -> list[str]:
    fragments: list[str] = []
    collecting = False

    for line in lines:
        lowered = line.lower().strip(":")
        if lowered in _REVIEW_HEADINGS:
            collecting = True
            continue
        if collecting and (lowered.startswith("вопрос") or _is_competitor_heading(line)):
            break
        if collecting and len(line) >= 24 and not re.search(r"^\d+\s*(отзыв|оцен)", lowered):
            fragments.append(line.strip())
        if len(fragments) >= 5:
            break

    return fragments


def _extract_competitor_cards(lines: list[str]) -> list[CompetitorCard]:
    cards: list[CompetitorCard] = []
    current: dict[str, int | float | str | None] | None = None

    for line in lines:
        if not line.strip() or _is_competitor_heading(line) or _is_junk_line(line):
            continue
        if _is_stop_after_competitors(line):
            break

        price = _parse_price(line)
        rating = _extract_rating(line)
        reviews = _extract_review_count(line)
        name_candidate = _candidate_competitor_name(line)

        if name_candidate:
            if current and isinstance(current.get("name"), str):
                cards.append(_competitor_from_dict(current, len(cards) + 1))
            current = {
                "name": name_candidate,
                "price": price,
                "old_price": None,
                "rating": rating,
                "review_count": reviews,
            }
            continue

        if current is None:
            continue
        if price is not None:
            if "стара" in line.lower() or "до скидки" in line.lower():
                current["old_price"] = price
            elif current.get("price") is None:
                current["price"] = price
            elif isinstance(current.get("price"), int) and price > int(current["price"]):
                current["old_price"] = price
        if rating is not None and current.get("rating") is None:
            current["rating"] = rating
        if reviews is not None and current.get("review_count") is None:
            current["review_count"] = reviews

    if current and isinstance(current.get("name"), str):
        cards.append(_competitor_from_dict(current, len(cards) + 1))

    return cards[:10]


def _candidate_competitor_name(line: str) -> str | None:
    candidate = _PRICE_RE.sub("", line)
    candidate = _RATING_RE.sub("", candidate)
    candidate = re.sub(r"\b\d+\s*(?:отзыв(?:а|ов)?|оцен(?:ка|ки|ок))\b", "", candidate, flags=re.IGNORECASE)
    candidate = _clean_name(candidate)
    if _is_title_candidate(candidate):
        return candidate
    return None


def _is_stop_after_competitors(line: str) -> bool:
    lowered = line.lower()
    return lowered.startswith(("об ozon", "об wildberries", "покупателям", "продавцам"))


def _competitor_from_dict(data: dict[str, int | float | str | None], position: int) -> CompetitorCard:
    return CompetitorCard(
        name=str(data["name"]),
        price=data["price"] if isinstance(data.get("price"), int) else None,
        old_price=data["old_price"] if isinstance(data.get("old_price"), int) else None,
        rating=data["rating"] if isinstance(data.get("rating"), float) else None,
        review_count=data["review_count"] if isinstance(data.get("review_count"), int) else None,
        position=position,
    )


def _build_missing_blocks(snapshot: MarketplaceCardSnapshot) -> list[str]:
    checks = {
        "platform": snapshot.platform == "unknown",
        "product_name": snapshot.product_name is None,
        "brand": snapshot.brand is None,
        "sku": snapshot.sku is None,
        "category_path": not snapshot.category_path,
        "current_price": snapshot.current_price is None,
        "old_price": snapshot.old_price is None,
        "unit_price": snapshot.unit_price is None,
        "rating": snapshot.rating is None,
        "review_count": snapshot.review_count is None,
        "question_count": snapshot.question_count is None,
        "seller_name": snapshot.seller_name is None,
        "variants": not snapshot.variants,
        "characteristics": not snapshot.characteristics,
        "description": snapshot.description is None,
        "review_fragments": not snapshot.review_fragments,
        "competitor_cards": not snapshot.competitor_cards,
    }
    return [name for name, is_missing in checks.items() if is_missing]
