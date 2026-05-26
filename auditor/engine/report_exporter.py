from __future__ import annotations

import re
import unicodedata
from datetime import date

from auditor.engine.generator import AuditItem, AuditReport

SECTION_TITLES: dict[str, str] = {
    "title": "Заголовок",
    "price_competitors": "Цена и конкурентная полка",
    "competitors": "Конкуренты",
    "description": "Описание",
    "characteristics": "Характеристики",
    "specs": "Характеристики",
    "attributes": "Характеристики",
    "seo": "SEO",
    "reviews_risks": "Отзывы и риски",
    "reviews": "Отзывы и риски",
    "risks": "Отзывы и риски",
    "photos": "Фото, инфографика и видео",
    "photo_video": "Фото, инфографика и видео",
    "media": "Фото, инфографика и видео",
    "gallery": "Фото, инфографика и видео",
    "video": "Фото, инфографика и видео",
}

PRIORITY_ORDER: dict[str, int] = {"red": 0, "yellow": 1, "green": 2}
PRIORITY_LABELS: dict[str, str] = {
    "red": "Критично",
    "yellow": "Важно",
    "green": "Желательно",
}

PRICE_SECTIONS: tuple[str, ...] = ("price_competitors", "competitors")
CHARACTERISTICS_SECTIONS: tuple[str, ...] = ("characteristics", "specs", "attributes")
REVIEWS_SECTIONS: tuple[str, ...] = ("reviews_risks", "reviews", "risks")
MEDIA_SECTIONS: tuple[str, ...] = ("photos", "photo_video", "media", "gallery", "video")
EXPORT_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Заголовок", ("title",)),
    ("Цена и конкурентная полка", PRICE_SECTIONS),
    ("Описание", ("description",)),
    ("Характеристики", CHARACTERISTICS_SECTIONS),
    ("SEO", ("seo",)),
    ("Отзывы и риски", REVIEWS_SECTIONS),
)

TRANSLIT: dict[int, str] = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "i",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "c",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
        "А": "A",
        "Б": "B",
        "В": "V",
        "Г": "G",
        "Д": "D",
        "Е": "E",
        "Ё": "E",
        "Ж": "Zh",
        "З": "Z",
        "И": "I",
        "Й": "I",
        "К": "K",
        "Л": "L",
        "М": "M",
        "Н": "N",
        "О": "O",
        "П": "P",
        "Р": "R",
        "С": "S",
        "Т": "T",
        "У": "U",
        "Ф": "F",
        "Х": "H",
        "Ц": "C",
        "Ч": "Ch",
        "Ш": "Sh",
        "Щ": "Sch",
        "Ъ": "",
        "Ы": "Y",
        "Ь": "",
        "Э": "E",
        "Ю": "Yu",
        "Я": "Ya",
    }
)


def extract_top_actions(report: AuditReport, limit: int = 3) -> list[str]:
    if limit <= 0:
        return []

    actions: list[str] = []
    seen: set[str] = set()
    sorted_items = sorted(
        enumerate(report.items),
        key=lambda pair: (PRIORITY_ORDER.get(pair[1].priority, 9), pair[0]),
    )

    for _, item in sorted_items:
        candidate = _clean_line(item.recommendation or item.finding)
        if not candidate or candidate in seen:
            continue
        actions.append(candidate)
        seen.add(candidate)
        if len(actions) >= limit:
            return actions

    for candidate in _split_summary_actions(report.summary):
        action = _clean_line(candidate)
        if not action or action in seen:
            continue
        actions.append(action)
        seen.add(action)
        if len(actions) >= limit:
            break

    for candidate in _fallback_actions():
        action = _clean_line(candidate)
        if action in seen:
            continue
        actions.append(action)
        seen.add(action)
        if len(actions) >= limit:
            break

    return actions


def build_unchecked_block(report: AuditReport, media_added: bool = False) -> list[str]:
    unchecked: list[str] = []

    if not media_added:
        unchecked.extend(
            [
                "Фото: не проверены, потому что пользователь не добавил медиа.",
                "Инфографика: не проверена, потому что пользователь не добавил медиа.",
                "Видео: не проверено, потому что пользователь не добавил описание или файл.",
                "Порядок галереи: не проверен без фото и видео по позициям.",
            ]
        )

    sections = {item.section for item in report.items}
    if not sections.intersection(PRICE_SECTIONS):
        unchecked.append("Конкурентная полка: нет отдельной секции с ценой и конкурентами.")
    if not sections.intersection(REVIEWS_SECTIONS):
        unchecked.append("Отзывы и риски: нет отдельной секции в AuditReport.")

    unchecked.append(
        "Точный рост продаж, CTR, конверсия и позиции в поиске: не прогнозировались."
    )
    return unchecked


def export_audit_report_text(report: AuditReport, media_added: bool = False) -> str:
    product_name = _clean_line(report.product_name) or "не указан"
    platform = _format_platform(report.platform)
    score = f"{report.overall_score}/100" if report.overall_score else "нет оценки"
    top_actions = extract_top_actions(report, limit=3)

    lines: list[str] = [
        "AI-АУДИТ КАРТОЧКИ WB/OZON",
        "",
        f"Товар: {product_name}",
        f"Площадка: {platform}",
        f"Дата: {date.today().isoformat()}",
        f"Общий балл: {score}",
        "",
        "Краткий вывод: 3 главных действия",
    ]

    if top_actions:
        for index, action in enumerate(top_actions, start=1):
            lines.append(f"{index}. {action}")
    else:
        lines.append("1. Проверить заполненность ключевых блоков карточки вручную.")

    if report.summary:
        lines.extend(["", "Краткий итог", _clean_paragraph(report.summary)])

    for title, section_keys in EXPORT_SECTIONS:
        _append_items_section(lines, title, _items_for_sections(report, section_keys))
        if title == "Цена и конкурентная полка" and report.competitor_insight:
            if not _items_for_sections(report, section_keys):
                lines.extend(["", title])
            lines.append(
                f"- Конкурентная полка: {_clean_paragraph(report.competitor_insight)}"
            )

    media_items = _items_for_sections(report, MEDIA_SECTIONS)
    if media_items or media_added:
        _append_items_section(lines, "Фото, инфографика и видео", media_items)
        if media_added and not media_items:
            lines.extend(
                [
                    "",
                    "Фото, инфографика и видео",
                    "- Медиа добавлены, но отдельные замечания по ним в AuditReport не указаны.",
                ]
            )

    lines.extend(["", "Что исправить в первую очередь"])
    if top_actions:
        for index, action in enumerate(top_actions, start=1):
            lines.append(f"{index}. {action}")
    else:
        lines.append("1. Собрать недостающие данные и повторить аудит.")

    lines.extend(["", "Что не проверено"])
    for unchecked in build_unchecked_block(report, media_added=media_added):
        lines.append(f"- {unchecked}")

    return "\n".join(lines).strip() + "\n"


def build_report_filename(report: AuditReport) -> str:
    if not report.product_name.strip():
        return "audit_report.txt"

    stem = report.product_name.translate(TRANSLIT)
    stem = unicodedata.normalize("NFKD", stem)
    stem = stem.encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_-").lower()
    if not stem:
        return "audit_report.txt"

    max_stem_length = 80 - len(".txt")
    stem = stem[:max_stem_length].rstrip("_-") or "audit_report"
    return f"{stem}.txt"


def _items_for_sections(report: AuditReport, section_keys: tuple[str, ...]) -> list[AuditItem]:
    section_set = set(section_keys)
    return [item for item in report.items if item.section in section_set]


def _append_items_section(lines: list[str], title: str, items: list[AuditItem]) -> None:
    if not items:
        return

    lines.extend(["", title])
    for item in items:
        priority = PRIORITY_LABELS.get(item.priority, item.priority or "Без приоритета")
        finding = _clean_line(item.finding)
        recommendation = _clean_line(item.recommendation)
        why = _clean_line(item.why)
        section_label = SECTION_TITLES.get(item.section, item.section)

        if finding:
            lines.append(f"- [{priority}] {finding}")
        if recommendation:
            lines.append(f"  Исправить: {recommendation}")
        if why:
            lines.append(f"  Почему важно: {why}")
        if section_label and section_label != title:
            lines.append(f"  Секция: {section_label}")


def _format_platform(platform: str) -> str:
    platform_names = {
        "wb": "Wildberries",
        "ozon": "Ozon",
        "unknown": "не определена",
        "": "не указана",
    }
    return platform_names.get(platform, platform)


def _split_summary_actions(summary: str) -> list[str]:
    if not summary.strip():
        return []
    normalized = re.sub(r"\s+", " ", summary.strip())
    numbered = re.split(r"(?:^|\s)\d+[.)]\s+", normalized)
    if len(numbered) > 1:
        return [part.strip(" .;") for part in numbered[1:] if part.strip(" .;")]
    return [part.strip(" .;") for part in re.split(r"[.;]\s+", normalized) if part.strip()]


def _fallback_actions() -> list[str]:
    return [
        "Проверить заголовок: он должен быстро объяснять товар и главное отличие.",
        "Сверить цену и конкурентную полку по ближайшим товарам.",
        "Заполнить описание, характеристики, SEO и отзывы без обещаний точных метрик.",
    ]


def _clean_line(text: str) -> str:
    return _neutralize_exact_promises(re.sub(r"\s+", " ", text.strip()))


def _clean_paragraph(text: str) -> str:
    lines = [_clean_line(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _neutralize_exact_promises(text: str) -> str:
    metric_words = r"(?:продаж\w*|ctr|конверси\w*|выкуп\w*|ранжировани\w*|позици\w*)"
    patterns = (
        rf"(?i)\+?\d+(?:[,.]\d+)?\s*%\s*{metric_words}",
        rf"(?i){metric_words}\s*\+?\d+(?:[,.]\d+)?\s*%",
        rf"(?i)\+?\d+(?:[,.]\d+)?\s*%[^.\n]{{0,45}}{metric_words}",
        rf"(?i){metric_words}[^.\n]{{0,45}}\+?\d+(?:[,.]\d+)?\s*%",
        r"(?i)(?:гарантированно|точно)\s+[^.\n]{0,60}"
        rf"{metric_words}",
        r"(?i)(?:попад[а-я]*|вывед[а-я]*|подним[а-я]*)[^.\n]{0,45}"
        r"(?:топ|top|позици\w*|место)",
    )
    cleaned = text
    for pattern in patterns:
        cleaned = re.sub(pattern, "может улучшить показатели карточки", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()
