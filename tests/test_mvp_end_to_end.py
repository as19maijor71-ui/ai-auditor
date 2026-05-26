from __future__ import annotations

import asyncio
import inspect
import os
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ.setdefault("BOT_TOKEN", "test-token")

from auditor.bot import handlers  # noqa: E402
from auditor.config import settings  # noqa: E402
from auditor.engine import audit_runner, media_runner  # noqa: E402
from auditor.engine.generator import AuditItem, AuditReport  # noqa: E402
from auditor.engine.media_models import MediaItem  # noqa: E402
from auditor.engine.paste_models import LocalAuditFacts, MarketplaceCardSnapshot  # noqa: E402
from auditor.engine.report_exporter import export_audit_report_text  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "paste"


class _NetworkBlockedAsyncClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("Real network calls are blocked in MVP e2e tests")


class _FakeUser:
    def __init__(self, user_id: int, username: str = "tester") -> None:
        self.id = user_id
        self.username = username
        self.full_name = "Test User"


class _FakeMessage:
    def __init__(
        self,
        user_id: int = 1001,
        *,
        text: str | None = None,
        document: object | None = None,
        photo: list[object] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.from_user = _FakeUser(user_id)
        self.text = text
        self.document = document
        self.photo = photo or []
        self.events = events if events is not None else []

    async def answer(self, text: str, **kwargs: Any) -> "_FakeMessage":
        self.events.append({"kind": "answer", "text": text, "kwargs": kwargs})
        return _FakeMessage(self.from_user.id, events=self.events)

    async def answer_document(self, document: object, **kwargs: Any) -> "_FakeMessage":
        self.events.append(
            {"kind": "document", "document": document, "kwargs": kwargs}
        )
        return _FakeMessage(self.from_user.id, events=self.events)

    async def delete(self) -> None:
        self.events.append({"kind": "delete"})

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.events.append({"kind": "edit_text", "text": text, "kwargs": kwargs})


class _FakeCallback:
    def __init__(self, user_id: int = 1001, *, events: list[dict[str, Any]]) -> None:
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessage(user_id, events=events)
        self.data = ""
        self.events = events

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.events.append(
            {"kind": "callback_answer", "text": text, "show_alert": show_alert}
        )


class _FakeState:
    def __init__(
        self,
        state: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self._state = state
        self.data = data.copy() if data else {}

    async def get_state(self) -> str | None:
        return self._state

    async def set_state(self, state: object) -> None:
        self._state = getattr(state, "state", str(state))

    async def get_data(self) -> dict[str, Any]:
        return self.data.copy()

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def clear(self) -> None:
        self._state = None
        self.data.clear()


class _FakeStorage:
    def __init__(
        self,
        *,
        events: list[dict[str, Any]],
        free_audits_used: int = 0,
        whitelisted: bool = False,
    ) -> None:
        self.events = events
        self.free_audits_used = free_audits_used
        self.whitelisted = whitelisted
        self.copy_cache: dict[str, str] = {}

    def is_whitelisted(self, user_id: int) -> bool:
        return self.whitelisted

    def has_free_audits(self, user_id: int) -> bool:
        return self.free_audits_used < settings.FREE_AUDIT_LIMIT

    def get_usage(self, user_id: int) -> int:
        return self.free_audits_used

    def increment_usage(self, user_id: int) -> int:
        self.events.append({"kind": "usage_increment", "user_id": user_id})
        self.free_audits_used += 1
        return self.free_audits_used

    def log_audit(
        self,
        user_id: int,
        username: str | None,
        url: str,
        platform: str,
        score: int,
    ) -> None:
        self.events.append(
            {
                "kind": "audit_log",
                "user_id": user_id,
                "url": url,
                "platform": platform,
                "score": score,
            }
        )

    def store_copy_data(self, key: str, text: str) -> None:
        self.events.append({"kind": "copy_cache", "key": key})
        self.copy_cache[key] = text


class _FakeDocument:
    def __init__(self, file_name: str, data: bytes) -> None:
        self.file_name = file_name
        self.file_id = "doc-1"
        self.file_size = len(data)


class _FakePhoto:
    def __init__(self, file_id: str, file_size: int = 1024) -> None:
        self.file_id = file_id
        self.file_size = file_size


class _FakeTelegramFile:
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path


class _FakeBot:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.get_file_calls = 0
        self.download_calls = 0

    async def get_file(self, file_id: str) -> _FakeTelegramFile:
        self.get_file_calls += 1
        return _FakeTelegramFile(file_id)

    async def download_file(self, file_path: str) -> BytesIO:
        self.download_calls += 1
        return BytesIO(self.files[file_path])


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_runner.httpx, "AsyncClient", _NetworkBlockedAsyncClient)
    monkeypatch.setattr(media_runner.httpx, "AsyncClient", _NetworkBlockedAsyncClient)


@pytest.fixture(autouse=True)
def _disable_thinking_animation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop_animation(msg: object) -> None:
        return None

    monkeypatch.setattr(handlers, "_animate_thinking", noop_animation)


def _read_fixture(filename: str) -> str:
    return (FIXTURE_DIR / filename).read_text(encoding="utf-8")


def _split_text(text: str, parts_count: int) -> list[str]:
    chunk_size = max(1, len(text) // parts_count)
    chunks = [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]
    return chunks[: parts_count - 1] + ["".join(chunks[parts_count - 1 :])]


def _quick_report(platform: str = "", product_name: str = "") -> AuditReport:
    sections = (
        "title",
        "price_competitors",
        "description",
        "seo",
        "reviews_risks",
    )
    return AuditReport(
        platform=platform,
        product_name=product_name,
        overall_score=81,
        summary=(
            "Быстрый аудит без проверки фото/видео. "
            "1. Уточнить заголовок. 2. Усилить описание. 3. Сверить цену."
        ),
        competitor_insight="В копипасте есть конкурентная полка.",
        items=[
            AuditItem(
                section=section,
                priority="green" if section != "title" else "yellow",
                finding=f"Факт для {section}.",
                recommendation=f"Действие для {section}.",
                why=f"Причина для {section}.",
            )
            for section in sections
        ],
    )


def _last_keyboard_callbacks(events: list[dict[str, Any]]) -> set[str]:
    for event in reversed(events):
        markup = event.get("kwargs", {}).get("reply_markup")
        if markup is None:
            continue
        return {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }
    return set()


def _event_kinds(events: list[dict[str, Any]]) -> list[str]:
    return [str(event["kind"]) for event in events]


def _contains_marker(value: object, marker: str) -> bool:
    if isinstance(value, bytes):
        return marker.encode("utf-8") in value
    if isinstance(value, str):
        return marker in value
    if isinstance(value, dict):
        return any(_contains_marker(item, marker) for item in value.values())
    if isinstance(value, list):
        return any(_contains_marker(item, marker) for item in value)
    return False


def test_wb_paste_parts_creates_quick_report_txt_and_media_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []
    user_id = 501
    storage = _FakeStorage(events=events)
    state = _FakeState()
    callback = _FakeCallback(user_id, events=events)
    captured: dict[str, MarketplaceCardSnapshot | LocalAuditFacts] = {}

    async def fake_run_quick_text_audit(
        snapshot: MarketplaceCardSnapshot,
        facts: LocalAuditFacts,
    ) -> AuditReport:
        captured["snapshot"] = snapshot
        captured["facts"] = facts
        return _quick_report()

    monkeypatch.setattr(handlers, "_storage_instance", storage)
    monkeypatch.setattr(handlers, "run_quick_text_audit", fake_run_quick_text_audit)

    asyncio.run(handlers.paste_start_cb(callback, state))
    for part in _split_text(_read_fixture("wb_02_coffee_food.txt"), 3):
        asyncio.run(
            handlers.paste_text_received(
                _FakeMessage(user_id, text=part, events=events),
                state,
            )
        )
    asyncio.run(handlers.paste_run_cb(callback, state))

    snapshot = captured["snapshot"]
    assert isinstance(snapshot, MarketplaceCardSnapshot)
    assert snapshot.platform == "wb"
    assert snapshot.product_name == "Кофе в зернах Brazil Santos 1 кг арабика"
    assert any(event["kind"] == "document" for event in events)
    assert "media_next_step" in _last_keyboard_callbacks(events)
    assert any(
        event["kind"] == "answer" and "3 главных действия" in event["text"]
        for event in events
    )


def test_ozon_txt_flow_detects_snapshot_and_charges_after_successful_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []
    user_id = 502
    fixture = _read_fixture("ozon_02_matcha_food.txt").encode("utf-8")
    storage = _FakeStorage(events=events)
    state = _FakeState(
        handlers.AuditFlow.collecting_paste.state,
        {
            "paste_text": "",
            "paste_parts": 0,
            "paste_truncated": False,
            "paste_source_type": "paste",
        },
    )
    captured: dict[str, MarketplaceCardSnapshot] = {}

    async def fake_run_quick_text_audit(
        snapshot: MarketplaceCardSnapshot,
        facts: LocalAuditFacts,
    ) -> AuditReport:
        captured["snapshot"] = snapshot
        return _quick_report(platform="", product_name="")

    monkeypatch.setattr(handlers, "_storage_instance", storage)
    monkeypatch.setattr(handlers, "run_quick_text_audit", fake_run_quick_text_audit)

    document = _FakeDocument("ozon_matcha.txt", fixture)
    message = _FakeMessage(user_id, document=document, events=events)
    bot = _FakeBot({"doc-1": fixture})

    asyncio.run(handlers.paste_txt_received(message, state, bot))
    assert storage.get_usage(user_id) == 0

    callback = _FakeCallback(user_id, events=events)
    asyncio.run(handlers.paste_run_cb(callback, state))

    snapshot = captured["snapshot"]
    assert snapshot.platform == "ozon"
    assert snapshot.source_type == "txt_file"
    assert snapshot.product_name == "Чай матча японский порошковый церемониальный 100 г"

    saved_report = state.data["last_audit_report"]
    assert saved_report["platform"] == "ozon"
    assert saved_report["product_name"] == snapshot.product_name
    assert any(event["kind"] == "document" for event in events)

    kinds = _event_kinds(events)
    assert kinds.count("usage_increment") == 1
    assert kinds.index("document") < kinds.index("usage_increment")


def test_media_flow_five_photos_and_video_updates_report_without_second_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []
    user_id = 503
    storage = _FakeStorage(
        events=events,
        free_audits_used=settings.FREE_AUDIT_LIMIT,
    )
    base_report = _quick_report(platform="wb", product_name="Кофе в зернах")
    state = _FakeState(
        handlers.AuditFlow.waiting_url.state,
        {
            "last_audit_report": base_report.model_dump(),
            "last_audit_user_id": user_id,
            "last_report_media_added": False,
        },
    )
    media_calls: list[tuple[bytes, int]] = []
    media_types = ["main", "lifestyle", "infographic", "composition", "packaging"]
    verdicts = ["keep", "keep", "move", "keep", "remove"]

    async def fake_call_gemini_media_audit(
        image_data: bytes,
        position: int,
    ) -> MediaItem:
        media_calls.append((image_data, position))
        return MediaItem(
            position=position,
            ocr_text=f"Текст фото {position}",
            visual_summary=f"Фото {position} показывает отдельный смысловой слайд.",
            media_type=media_types[position - 1],  # type: ignore[arg-type]
            preliminary_verdict=verdicts[position - 1],  # type: ignore[arg-type]
        )

    monkeypatch.setattr(handlers, "_storage_instance", storage)
    monkeypatch.setattr(handlers, "call_gemini_media_audit", fake_call_gemini_media_audit)

    callback = _FakeCallback(user_id, events=events)
    asyncio.run(handlers.media_next_step_cb(callback, state))

    bot = _FakeBot(
        {
            f"photo-{position}": f"raw-image-{position}-secret".encode("utf-8")
            for position in range(1, 6)
        }
    )
    for position in range(1, 6):
        asyncio.run(
            handlers.media_photo_received(
                _FakeMessage(
                    user_id,
                    photo=[_FakePhoto(f"photo-{position}")],
                    events=events,
                ),
                state,
                bot,
            )
        )

    asyncio.run(handlers.media_describe_video_cb(callback, state))
    asyncio.run(
        handlers.media_video_description_received(
            _FakeMessage(
                user_id,
                text=(
                    "Видео 1\n"
                    "Позиция в галерее: 6\n"
                    "Длительность: 45 сек\n"
                    "Что показано: приготовление кофе и крупный план упаковки.\n"
                    "Тип видео: использование продукта\n"
                    "Качество: светлое, товар хорошо видно\n"
                    "Есть ли текст на экране: да\n"
                    "Есть ли призыв к покупке: нет\n"
                ),
                events=events,
            ),
            state,
        )
    )
    asyncio.run(handlers.media_photos_done_cb(callback, state))

    assert [position for _data, position in media_calls] == [1, 2, 3, 4, 5]
    assert "usage_increment" not in _event_kinds(events)

    saved_report = AuditReport.model_validate(state.data["last_audit_report"])
    media_items = [item for item in saved_report.items if item.section == "media"]
    assert len(media_items) == 6
    assert state.data["last_report_media_added"] is True

    full_text = export_audit_report_text(saved_report, media_added=True)
    unchecked_block = full_text.split("Что не проверено", 1)[1]
    assert "Фото:" not in unchecked_block
    assert "Инфографика:" not in unchecked_block
    assert "Видео:" not in unchecked_block
    assert "Порядок галереи:" not in unchecked_block
    assert "media_next_step" not in _last_keyboard_callbacks(events)

    for position in range(1, 6):
        marker = f"raw-image-{position}-secret"
        assert not _contains_marker(state.data, marker)
        assert not _contains_marker(storage.copy_cache, marker)


def test_quick_audit_limit_blocks_before_ai_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []
    user_id = 504
    storage = _FakeStorage(
        events=events,
        free_audits_used=settings.FREE_AUDIT_LIMIT,
    )
    state = _FakeState(
        handlers.AuditFlow.collecting_paste.state,
        {
            "paste_text": _read_fixture("wb_02_coffee_food.txt"),
            "paste_parts": 1,
            "paste_truncated": False,
            "paste_source_type": "paste",
        },
    )
    ai_called = False

    async def fake_run_quick_text_audit(
        snapshot: MarketplaceCardSnapshot,
        facts: LocalAuditFacts,
    ) -> AuditReport:
        nonlocal ai_called
        ai_called = True
        return _quick_report()

    monkeypatch.setattr(handlers, "_storage_instance", storage)
    monkeypatch.setattr(handlers, "run_quick_text_audit", fake_run_quick_text_audit)

    asyncio.run(handlers.paste_run_cb(_FakeCallback(user_id, events=events), state))

    assert ai_called is False
    assert any("Лимит бесплатных аудитов" in event.get("text", "") for event in events)


def test_media_ocr_limit_blocks_before_gemini_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []
    user_id = 505
    storage = _FakeStorage(
        events=events,
        free_audits_used=settings.FREE_AUDIT_LIMIT,
    )
    state = _FakeState(
        handlers.AuditFlow.collecting_media.state,
        {"media_photo_count": 0},
    )
    ocr_called = False

    async def fake_call_gemini_media_audit(
        image_data: bytes,
        position: int,
    ) -> MediaItem:
        nonlocal ocr_called
        ocr_called = True
        return MediaItem(
            position=position,
            ocr_text="",
            visual_summary="Фото товара.",
            media_type="main",
            preliminary_verdict="keep",
        )

    monkeypatch.setattr(handlers, "_storage_instance", storage)
    monkeypatch.setattr(handlers, "call_gemini_media_audit", fake_call_gemini_media_audit)
    bot = _FakeBot({"photo-limit": b"image-that-must-not-be-downloaded"})

    asyncio.run(
        handlers.media_photo_received(
            _FakeMessage(user_id, photo=[_FakePhoto("photo-limit")], events=events),
            state,
            bot,
        )
    )

    assert ocr_called is False
    assert bot.get_file_calls == 0
    assert bot.download_calls == 0
    assert any("Лимит бесплатных аудитов" in event.get("text", "") for event in events)


def test_too_short_text_does_not_start_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []
    user_id = 506
    state = _FakeState(
        handlers.AuditFlow.collecting_paste.state,
        {"paste_text": "x" * 299, "paste_source_type": "paste"},
    )
    ai_called = False

    async def fake_run_quick_text_audit(
        snapshot: MarketplaceCardSnapshot,
        facts: LocalAuditFacts,
    ) -> AuditReport:
        nonlocal ai_called
        ai_called = True
        return _quick_report()

    monkeypatch.setattr(handlers, "_storage_instance", _FakeStorage(events=events))
    monkeypatch.setattr(handlers, "run_quick_text_audit", fake_run_quick_text_audit)

    asyncio.run(handlers.paste_run_cb(_FakeCallback(user_id, events=events), state))

    assert ai_called is False
    assert any(
        event["kind"] == "callback_answer"
        and event["show_alert"] is True
        and "Недостаточно текста" in event["text"]
        for event in events
    )


def test_garbage_text_does_not_start_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []
    user_id = 507
    state = _FakeState(
        handlers.AuditFlow.collecting_paste.state,
        {
            "paste_text": ("абракадабра не карточка ***\n" * 40),
            "paste_source_type": "paste",
        },
    )
    ai_called = False

    async def fake_run_quick_text_audit(
        snapshot: MarketplaceCardSnapshot,
        facts: LocalAuditFacts,
    ) -> AuditReport:
        nonlocal ai_called
        ai_called = True
        return _quick_report()

    monkeypatch.setattr(handlers, "_storage_instance", _FakeStorage(events=events))
    monkeypatch.setattr(handlers, "run_quick_text_audit", fake_run_quick_text_audit)

    asyncio.run(handlers.paste_run_cb(_FakeCallback(user_id, events=events), state))

    assert ai_called is False
    assert any("не удалось определить карточку" in event.get("text", "").lower() for event in events)
    assert "last_audit_report" not in state.data


def test_unknown_platform_does_not_hallucinate_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []
    user_id = 508
    unknown_platform_text = (
        "Название товара: Товар без площадки 1 кг\n"
        "Цена: 1000 ₽\n"
        "Описание товара\n"
        "Подробное описание товара без маркеров WB или Ozon.\n"
        "Характеристики\n"
        "Вес: 1 кг\n"
    ) * 4
    state = _FakeState(
        handlers.AuditFlow.collecting_paste.state,
        {
            "paste_text": unknown_platform_text,
            "paste_source_type": "paste",
        },
    )
    ai_called = False

    async def fake_run_quick_text_audit(
        snapshot: MarketplaceCardSnapshot,
        facts: LocalAuditFacts,
    ) -> AuditReport:
        nonlocal ai_called
        ai_called = True
        return _quick_report(platform="wb", product_name="Выдуманный товар")

    monkeypatch.setattr(handlers, "_storage_instance", _FakeStorage(events=events))
    monkeypatch.setattr(handlers, "run_quick_text_audit", fake_run_quick_text_audit)

    asyncio.run(handlers.paste_run_cb(_FakeCallback(user_id, events=events), state))

    assert ai_called is False
    assert state.data["paste_snapshot_dump"]["platform"] == "unknown"
    assert "last_audit_report" not in state.data
    assert any("ai не запускаю" in event.get("text", "").lower() for event in events)


def test_security_providers_read_secrets_from_settings_and_do_not_log_keys() -> None:
    provider_source = "\n".join(
        inspect.getsource(function)
        for function in (
            audit_runner.call_gemini_quick_audit,
            audit_runner.call_openai_quick_audit,
            media_runner.call_gemini_media_audit,
        )
    )

    assert "settings.GEMINI_API_KEY" in provider_source
    assert "settings.OPENAI_API_KEY" in provider_source
    assert "os.environ" not in provider_source
    assert "load_dotenv" not in provider_source

    request = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini?key=SECRET",
    )
    response = httpx.Response(403, request=request)
    with pytest.raises(media_runner.MediaAuditError) as exc_info:
        media_runner._raise_for_status(response)  # noqa: SLF001

    assert "403" in str(exc_info.value)
    assert "SECRET" not in str(exc_info.value)
