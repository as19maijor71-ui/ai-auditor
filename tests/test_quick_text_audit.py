from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from auditor.config import settings
from auditor.engine import audit_runner
from auditor.engine.audit_runner import (
    QuickAuditError,
    parse_quick_audit_response,
    run_quick_text_audit,
    validate_quick_audit_report,
)
from auditor.engine.generator import AuditItem, AuditReport
from auditor.engine.paste_models import LocalAuditFacts, MarketplaceCardSnapshot
from auditor.engine.paste_parser import build_local_audit_facts, parse_marketplace_paste
from auditor.templates.prompts import build_quick_text_audit_prompt


def _snapshot_with_facts(extra_description: str = "") -> tuple[MarketplaceCardSnapshot, LocalAuditFacts]:
    snapshot = parse_marketplace_paste(
        """Ozon
Название товара: Тестовый товар для кухни
Бренд: TestBrand
Цена: 1000 ₽
4,8 120 отзывов
Описание
Удобный товар для ежедневного использования. {extra_description}
Характеристики
Материал: сталь
Цвет: серый
Подобрали для вас
Конкурент первый
900 ₽
4,7 80 отзывов
""".format(extra_description=extra_description)
    )
    return snapshot, build_local_audit_facts(snapshot)


def _valid_report_json(priority_overrides: dict[str, str] | None = None) -> str:
    priorities = {
        "title": "red",
        "price_competitors": "yellow",
        "description": "yellow",
        "seo": "green",
        "reviews_risks": "green",
    }
    if priority_overrides:
        priorities.update(priority_overrides)

    data = {
        "url": "",
        "platform": "ozon",
        "product_name": "Тестовый товар для кухни",
        "overall_score": 72,
        "items": [
            {
                "section": section,
                "priority": priority,
                "finding": f"Факт для {section}.",
                "recommendation": f"Действие для {section}.",
                "why": f"Причина для {section}.",
            }
            for section, priority in priorities.items()
        ],
        "summary": "Быстрый аудит без проверки медиа. 1. Исправить заголовок. 2. Усилить описание. 3. Проверить цену.",
        "competitor_insight": "Есть один конкурент из копипаста, цена ниже на 100 ₽.",
    }
    return json.dumps(data, ensure_ascii=False)


def test_build_quick_text_audit_prompt_contains_structured_input_and_limits_cleaned_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "QUICK_AUDIT_CLEANED_TEXT_LIMIT", 180)
    tail = "TAIL_NOT_INCLUDED"
    snapshot, facts = _snapshot_with_facts()
    snapshot.cleaned_text = "а" * 300 + tail

    prompt = build_quick_text_audit_prompt(snapshot, facts)

    assert '"snapshot"' in prompt
    assert '"facts"' in prompt
    assert "Тестовый товар для кухни" in prompt
    assert "missing_blocks" in prompt
    assert "Конкурент первый" in prompt
    assert tail not in prompt
    assert "raw_text" not in prompt
    payload = json.loads(prompt.split("Структурированный вход:\n", 1)[1])
    assert "competitor_cards" not in payload["snapshot"]
    assert "missing_blocks" not in payload["snapshot"]
    assert payload["competitor_cards"]


def test_parse_quick_audit_response_accepts_valid_audit_report() -> None:
    report = parse_quick_audit_response(_valid_report_json())

    assert report.overall_score == 72
    assert {item.section for item in report.items} >= {
        "title",
        "price_competitors",
        "description",
        "seo",
        "reviews_risks",
    }


def test_parse_quick_audit_response_accepts_brace_inside_json_string() -> None:
    data = json.loads(_valid_report_json())
    data["items"][0]["finding"] = "Фигурная скобка } внутри текста рекомендации."

    report = parse_quick_audit_response(json.dumps(data, ensure_ascii=False))

    assert "}" in report.items[0].finding


def test_parse_quick_audit_response_rejects_invalid_json_instead_of_raw_report() -> None:
    with pytest.raises(QuickAuditError):
        parse_quick_audit_response("не json и не AuditReport")


def test_parse_quick_audit_response_rejects_invalid_platform() -> None:
    data = json.loads(_valid_report_json())
    data["platform"] = "wb | ozon | unknown"

    with pytest.raises(QuickAuditError, match="platform"):
        parse_quick_audit_response(json.dumps(data, ensure_ascii=False))


def test_validate_quick_audit_report_requires_five_mandatory_sections() -> None:
    report = AuditReport(
        overall_score=70,
        items=[
            AuditItem(
                section="title",
                priority="green",
                finding="Факт.",
                recommendation="Действие.",
                why="Причина.",
            )
        ],
    )

    with pytest.raises(QuickAuditError, match="обязательных секций"):
        validate_quick_audit_report(report)


def test_validate_quick_audit_report_rejects_more_than_three_red_items() -> None:
    report = AuditReport.model_validate_json(
        _valid_report_json(
            {
                "title": "red",
                "price_competitors": "red",
                "description": "red",
                "seo": "red",
                "reviews_risks": "yellow",
            }
        )
    )

    with pytest.raises(QuickAuditError, match="больше 3 red"):
        validate_quick_audit_report(report)


def test_run_quick_text_audit_gemini_success_does_not_call_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, facts = _snapshot_with_facts()
    openai_called = False

    async def fake_gemini(prompt: str) -> str:
        return _valid_report_json()

    async def fake_openai(prompt: str) -> str:
        nonlocal openai_called
        openai_called = True
        return _valid_report_json()

    monkeypatch.setattr(audit_runner, "call_gemini_quick_audit", fake_gemini)
    monkeypatch.setattr(audit_runner, "call_openai_quick_audit", fake_openai)

    report = asyncio.run(run_quick_text_audit(snapshot, facts))

    assert report.overall_score == 72
    assert openai_called is False


def test_run_quick_text_audit_gemini_invalid_json_retries_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, facts = _snapshot_with_facts()
    calls = 0
    openai_called = False

    async def fake_gemini(prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "invalid json" if calls == 1 else _valid_report_json()

    async def fake_openai(prompt: str) -> str:
        nonlocal openai_called
        openai_called = True
        return _valid_report_json()

    monkeypatch.setattr(audit_runner, "call_gemini_quick_audit", fake_gemini)
    monkeypatch.setattr(audit_runner, "call_openai_quick_audit", fake_openai)

    report = asyncio.run(run_quick_text_audit(snapshot, facts))

    assert report.overall_score == 72
    assert calls == 2
    assert openai_called is False


def test_run_quick_text_audit_gemini_failed_twice_uses_openai_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, facts = _snapshot_with_facts()
    gemini_calls = 0
    openai_calls = 0

    async def fake_gemini(prompt: str) -> str:
        nonlocal gemini_calls
        gemini_calls += 1
        raise QuickAuditError("gemini failed")

    async def fake_openai(prompt: str) -> str:
        nonlocal openai_calls
        openai_calls += 1
        return _valid_report_json()

    monkeypatch.setattr(audit_runner, "call_gemini_quick_audit", fake_gemini)
    monkeypatch.setattr(audit_runner, "call_openai_quick_audit", fake_openai)

    report = asyncio.run(run_quick_text_audit(snapshot, facts))

    assert report.overall_score == 72
    assert gemini_calls == 2
    assert openai_calls == 1


def test_run_quick_text_audit_both_providers_failed_raises_quick_audit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, facts = _snapshot_with_facts()

    async def fake_gemini(prompt: str) -> str:
        raise QuickAuditError("gemini failed")

    async def fake_openai(prompt: str) -> str:
        raise QuickAuditError("openai failed")

    monkeypatch.setattr(audit_runner, "call_gemini_quick_audit", fake_gemini)
    monkeypatch.setattr(audit_runner, "call_openai_quick_audit", fake_openai)

    with pytest.raises(QuickAuditError):
        asyncio.run(run_quick_text_audit(snapshot, facts))


def test_provider_http_error_message_does_not_leak_api_key() -> None:
    request = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent?key=SECRET",
    )
    response = httpx.Response(403, request=request)

    with pytest.raises(QuickAuditError) as exc_info:
        audit_runner._raise_for_status(response, "Gemini")  # noqa: SLF001

    assert "403" in str(exc_info.value)
    assert "SECRET" not in str(exc_info.value)
