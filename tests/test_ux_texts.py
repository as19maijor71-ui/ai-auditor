from __future__ import annotations

import inspect
import os

os.environ.setdefault("BOT_TOKEN", "test-token")

from auditor.bot.handlers import (  # noqa: E402
    _build_help_keyboard,
    _build_help_text,
    _build_start_caption,
    _build_start_keyboard,
)


def test_start_caption_explains_copy_paste_and_txt_flow() -> None:
    caption = _build_start_caption("📖 /help")

    assert "Ctrl+A" in caption
    assert "Ctrl+C" in caption
    assert ".txt" in caption


def test_start_keyboard_has_paste_as_first_button() -> None:
    keyboard = _build_start_keyboard()

    first_button = keyboard.inline_keyboard[0][0]

    assert first_button.text == "🧾 Вставить текст карточки"
    assert first_button.callback_data == "paste_start"


def test_start_caption_keeps_excel_csv_out_of_main_path() -> None:
    caption = _build_start_caption("📖 /help")

    main_index = caption.index("Основной способ")
    backup_index = caption.index("Запасные/старые способы")
    export_index = caption.index("Excel/CSV")

    assert main_index < backup_index < export_index
    assert "Excel/CSV" not in caption[main_index:backup_index]


def test_help_text_contains_main_way_block() -> None:
    text = _build_help_text()

    assert "Основной способ" in text
    assert "Ctrl+A" in text
    assert "Ctrl+C" in text
    assert ".txt" in text
    assert "Запустить быстрый аудит" in text


def test_help_text_contains_unchecked_media_block() -> None:
    text = _build_help_text()
    lower_text = text.lower()

    assert "Что не проверяется без медиа" in text
    assert "фото" in lower_text
    assert "инфографика" in lower_text
    assert "видео" in lower_text
    assert "порядок галереи" in lower_text


def test_help_text_lists_quick_audit_scope() -> None:
    text = _build_help_text().lower()

    assert "заголовок" in text
    assert "цена и конкурентная полка" in text
    assert "описание" in text
    assert "характеристики" in text
    assert "seo" in text
    assert "отзывы/риски" in text


def test_help_text_mentions_excel_csv_only_as_backup() -> None:
    text = _build_help_text()

    backup_index = text.index("Запасные способы")
    export_index = text.index("Excel/CSV")

    assert backup_index < export_index
    assert "Excel/CSV" not in text[:backup_index]


def test_help_helpers_are_static_and_do_not_need_ai_config_or_network() -> None:
    help_source = inspect.getsource(_build_help_text)
    keyboard_source = inspect.getsource(_build_help_keyboard)
    combined_source = help_source + keyboard_source

    forbidden_fragments = (
        "settings",
        "os.environ",
        "httpx",
        "audit_card",
        "run_quick_text_audit",
        "fetch_product_page",
        "call_vision",
    )

    for fragment in forbidden_fragments:
        assert fragment not in combined_source
