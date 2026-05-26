from __future__ import annotations

import asyncio
import json as jsonlib
import os

import pytest
from pydantic import ValidationError

os.environ.setdefault("BOT_TOKEN", "test-token")

from auditor.config import settings  # noqa: E402
from auditor.bot import handlers  # noqa: E402
from auditor.engine import media_runner  # noqa: E402
from auditor.engine.generator import AuditItem, AuditReport  # noqa: E402
from auditor.engine.media_models import MediaItem, VideoDescription  # noqa: E402
from auditor.engine.media_runner import (  # noqa: E402
    MediaAuditError,
    build_media_audit_items,
    call_gemini_media_audit,
    parse_media_item_response,
    parse_video_description,
)
from auditor.engine.report_exporter import export_audit_report_text  # noqa: E402


def _media_item(position: int = 1, verdict: str = "keep") -> MediaItem:
    return MediaItem(
        position=position,
        ocr_text="Размер 20 см",
        visual_summary=f"Фото {position} показывает товар на светлом фоне.",
        media_type="main" if position == 1 else "infographic",
        preliminary_verdict=verdict,
    )


def _report(items: list[AuditItem] | None = None) -> AuditReport:
    return AuditReport(
        platform="wb",
        product_name="Тестовый товар",
        overall_score=70,
        summary="Быстрый аудит без точных обещаний.",
        items=items or [
            AuditItem(
                section="title",
                priority="green",
                finding="Заголовок заполнен.",
                recommendation="Проверить читаемость.",
                why="Заголовок влияет на понимание товара.",
            )
        ],
    )


def test_media_item_validates_allowed_media_type_and_verdict() -> None:
    item = _media_item()

    assert item.media_type == "main"
    assert item.preliminary_verdict == "keep"

    with pytest.raises(ValidationError):
        MediaItem(
            position=1,
            ocr_text="",
            visual_summary="Фото товара.",
            media_type="bad_type",
            preliminary_verdict="keep",
        )

    with pytest.raises(ValidationError):
        MediaItem(
            position=1,
            ocr_text="",
            visual_summary="Фото товара.",
            media_type="main",
            preliminary_verdict="bad_verdict",
        )


def test_video_description_validates_video_type() -> None:
    video = VideoDescription(
        position=2,
        duration_seconds=30,
        shown_content="Показано использование товара.",
        video_type="product_usage",
    )

    assert video.video_type == "product_usage"

    with pytest.raises(ValidationError):
        VideoDescription(
            shown_content="Показано использование товара.",
            video_type="bad_type",
        )


def test_parse_video_description_parses_template() -> None:
    video = parse_video_description(
        """Видео 1
Позиция в галерее: 3
Длительность: 1 мин 20 сек
Что показано: Распаковка товара и демонстрация комплектации.
Тип видео: распаковка
Качество: светлое, товар хорошо видно
Есть ли текст на экране: да
Есть ли призыв к покупке: нет
"""
    )

    assert video.position == 3
    assert video.duration_seconds == 80
    assert video.shown_content == "Распаковка товара и демонстрация комплектации."
    assert video.video_type == "unboxing"
    assert video.quality_comment == "светлое, товар хорошо видно"
    assert video.has_on_screen_text is True
    assert video.has_call_to_action is False


def test_parse_video_description_rejects_empty_shown_content() -> None:
    with pytest.raises(MediaAuditError, match="Что показано"):
        parse_video_description(
            """Видео 1
Позиция в галерее: 2
Длительность: 30 сек
Что показано:
Тип видео: обзор товара
"""
        )


def test_parse_media_item_response_accepts_valid_json() -> None:
    response = jsonlib.dumps(
        {
            "position": 99,
            "ocr_text": "100% хлопок",
            "visual_summary": "Инфографика с составом.",
            "media_type": "composition",
            "preliminary_verdict": "move",
        },
        ensure_ascii=False,
    )

    item = parse_media_item_response(response, position=2)

    assert item.position == 2
    assert item.media_type == "composition"
    assert item.preliminary_verdict == "move"


def test_parse_media_item_response_rejects_invalid_json() -> None:
    with pytest.raises(MediaAuditError, match="невалидный JSON"):
        parse_media_item_response("не json", position=1)


def test_build_media_audit_items_creates_media_section_and_keeps_photo_order() -> None:
    items = build_media_audit_items(
        media_items=[_media_item(position=2, verdict="move"), _media_item(position=1)],
        videos=[],
    )

    assert [item.section for item in items] == ["media", "media"]
    assert "Фото 1" in items[0].finding
    assert "Фото 2" in items[1].finding
    assert items[1].priority == "yellow"


def test_build_media_audit_items_adds_video_items() -> None:
    video = VideoDescription(
        position=6,
        duration_seconds=45,
        shown_content="Товар используют в быту.",
        video_type="product_usage",
        has_call_to_action=False,
    )

    items = build_media_audit_items(media_items=[], videos=[video])

    assert len(items) == 1
    assert items[0].section == "media"
    assert "Видео 1" in items[0].finding
    assert items[0].priority == "yellow"


def test_media_added_true_removes_media_from_unchecked_and_includes_media_section() -> None:
    media_items = build_media_audit_items([_media_item()], [])
    text = export_audit_report_text(_report(media_items), media_added=True)
    unchecked_block = text.split("Что не проверено", 1)[1]

    assert "Фото, инфографика и видео" in text
    assert "Фото 1" in text
    assert "Фото:" not in unchecked_block
    assert "Инфографика:" not in unchecked_block
    assert "Видео:" not in unchecked_block
    assert "Порядок галереи:" not in unchecked_block


def test_media_added_false_keeps_media_in_unchecked() -> None:
    text = export_audit_report_text(_report(), media_added=False)
    unchecked_block = text.split("Что не проверено", 1)[1]

    assert "Фото:" in unchecked_block
    assert "Инфографика:" in unchecked_block
    assert "Видео:" in unchecked_block
    assert "Порядок галереи:" in unchecked_block


def test_call_gemini_media_audit_can_be_mocked_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": jsonlib.dumps(
                                        {
                                            "position": 4,
                                            "ocr_text": "",
                                            "visual_summary": "Главное фото товара.",
                                            "media_type": "main",
                                            "preliminary_verdict": "keep",
                                        },
                                        ensure_ascii=False,
                                    )
                                }
                            ]
                        }
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(
            self,
            url: str,
            params: dict[str, str],
            json: dict,
        ) -> FakeResponse:
            calls.append({"url": url, "params": params, "json": json})
            return FakeResponse()

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(media_runner.httpx, "AsyncClient", FakeAsyncClient)

    item = asyncio.run(call_gemini_media_audit(b"fake-image", position=4))

    assert item.position == 4
    assert item.media_type == "main"
    assert len(calls) == 1
    parts = calls[0]["json"]["contents"][0]["parts"]
    assert "inlineData" in parts[1]
    assert parts[1]["inlineData"]["data"]


def test_media_limit_allows_saved_report_for_same_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeState:
        async def get_data(self) -> dict:
            return {
                "last_audit_user_id": 123,
                "last_audit_report": {"items": []},
            }

    monkeypatch.setattr(handlers, "_check_audit_limit", lambda user_id: "blocked")

    assert asyncio.run(handlers._check_media_audit_limit(123, FakeState())) is None
    assert asyncio.run(handlers._check_media_audit_limit(456, FakeState())) == "blocked"


def test_media_limit_blocks_saved_report_that_already_has_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeState:
        async def get_data(self) -> dict:
            return {
                "last_audit_user_id": 123,
                "last_audit_report": {"items": []},
                "last_report_media_added": True,
            }

    monkeypatch.setattr(handlers, "_check_audit_limit", lambda user_id: None)

    message = asyncio.run(handlers._check_media_audit_limit(123, FakeState()))

    assert message is not None
    assert "Медиа уже добавлены" in message


def test_report_actions_keyboard_hides_media_button_after_media_added() -> None:
    regular_keyboard = handlers._report_actions_keyboard("copy-key", media_added=False)
    media_keyboard = handlers._report_actions_keyboard("copy-key", media_added=True)

    regular_buttons = [
        button
        for row in regular_keyboard.inline_keyboard
        for button in row
    ]
    media_buttons = [
        button
        for row in media_keyboard.inline_keyboard
        for button in row
    ]

    assert "media_next_step" in {button.callback_data for button in regular_buttons}
    assert "media_next_step" not in {button.callback_data for button in media_buttons}
    assert "📸 Добавить фото/видео к отчёту" in {button.text for button in regular_buttons}
    assert "📸 Добавить фото/видео к отчёту" not in {button.text for button in media_buttons}
    assert "copy_audit:copy-key" in {button.callback_data for button in media_buttons}
    assert "back_to_start" in {button.callback_data for button in media_buttons}
