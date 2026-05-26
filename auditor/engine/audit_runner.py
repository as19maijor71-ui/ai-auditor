from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

import httpx

from auditor.config import settings
from auditor.engine.generator import AuditReport
from auditor.engine.paste_models import LocalAuditFacts, MarketplaceCardSnapshot
from auditor.templates.prompts import build_quick_text_audit_prompt

logger = logging.getLogger(__name__)

REQUIRED_QUICK_SECTIONS: tuple[str, ...] = (
    "title",
    "price_competitors",
    "description",
    "seo",
    "reviews_risks",
)
ALLOWED_PRIORITIES: set[str] = {"red", "yellow", "green"}
ALLOWED_PLATFORMS: set[str] = {"", "wb", "ozon", "unknown"}
MAX_RED_ITEMS: int = 3


class QuickAuditError(RuntimeError):
    """Raised when the quick AI audit cannot produce a valid AuditReport."""


def _extract_json_object(text: str) -> str:
    json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if json_match:
        fenced_text = json_match.group(1).strip()
        decoded = _decode_json_object_prefix(fenced_text)
        return decoded or fenced_text

    brace_start = text.find("{")
    if brace_start == -1:
        return text.strip()

    candidate = text[brace_start:].strip()
    decoded = _decode_json_object_prefix(candidate)
    if decoded:
        return decoded

    depth = 0
    for index, char in enumerate(candidate):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return candidate[: index + 1]
    return candidate


def _decode_json_object_prefix(text: str) -> str | None:
    try:
        parsed, end_index = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return text[:end_index].strip()


def _extract_gemini_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError) as exc:
        raise QuickAuditError("Gemini вернул ответ без текста отчёта.") from exc

    text_parts = [
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and part.get("text")
    ]
    if not text_parts:
        raise QuickAuditError("Gemini вернул пустой текст отчёта.")
    return "\n".join(text_parts)


def _extract_openai_text(data: dict) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise QuickAuditError("OpenAI вернул ответ без текста отчёта.") from exc

    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, Sequence):
        text_parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("text")
        ]
        if text_parts:
            return "\n".join(text_parts)
    raise QuickAuditError("OpenAI вернул пустой текст отчёта.")


def _retry_prompt(prompt: str) -> str:
    return (
        f"{prompt}\n\n"
        "Повторная попытка: предыдущий ответ не прошёл JSON/Pydantic-валидацию. "
        "Верни только один JSON-объект AuditReport с обязательными sections: "
        f"{', '.join(REQUIRED_QUICK_SECTIONS)}. Без markdown и текста вокруг JSON."
    )


def _raise_for_status(response: httpx.Response, provider: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        raise QuickAuditError(f"{provider} API вернул HTTP {status_code}.") from exc


async def call_gemini_quick_audit(prompt: str) -> str:
    if not settings.GEMINI_API_KEY:
        raise QuickAuditError("GEMINI_API_KEY не настроен.")

    url = (
        f"{settings.GEMINI_BASE_URL.rstrip('/')}/"
        f"{settings.GEMINI_TEXT_MODEL}:generateContent"
    )
    request_body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": settings.QUICK_AUDIT_MAX_TOKENS,
            "responseMimeType": "application/json",
        },
    }

    async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT) as client:
        response = await client.post(
            url,
            params={"key": settings.GEMINI_API_KEY},
            json=request_body,
        )
        _raise_for_status(response, "Gemini")
        return _extract_gemini_text(response.json())


async def call_openai_quick_audit(prompt: str) -> str:
    if not settings.OPENAI_API_KEY:
        raise QuickAuditError("OPENAI_API_KEY не настроен.")

    request_body = {
        "model": settings.OPENAI_TEXT_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты возвращаешь только валидный JSON AuditReport. "
                    "Без markdown, без пояснений, без текста вокруг JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": settings.QUICK_AUDIT_MAX_TOKENS,
    }

    async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT) as client:
        response = await client.post(
            settings.OPENAI_BASE_URL,
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=request_body,
        )
        _raise_for_status(response, "OpenAI")
        return _extract_openai_text(response.json())


def parse_quick_audit_response(response: str) -> AuditReport:
    json_text = _extract_json_object(response)
    try:
        report = AuditReport.model_validate_json(json_text)
    except Exception as exc:
        raise QuickAuditError("AI вернул невалидный JSON AuditReport.") from exc
    report.raw_response = ""
    return validate_quick_audit_report(report)


def validate_quick_audit_report(report: AuditReport) -> AuditReport:
    if not 0 <= report.overall_score <= 100:
        raise QuickAuditError("overall_score должен быть в диапазоне 0-100.")

    if report.platform not in ALLOWED_PLATFORMS:
        raise QuickAuditError("platform должен быть wb, ozon, unknown или пустой строкой.")

    priorities = {item.priority for item in report.items}
    invalid_priorities = priorities - ALLOWED_PRIORITIES
    if invalid_priorities:
        invalid = ", ".join(sorted(invalid_priorities))
        raise QuickAuditError(f"Недопустимый priority в AuditReport: {invalid}.")

    sections = {item.section for item in report.items}
    missing_sections = [
        section for section in REQUIRED_QUICK_SECTIONS if section not in sections
    ]
    if missing_sections:
        missing = ", ".join(missing_sections)
        raise QuickAuditError(f"В AuditReport нет обязательных секций: {missing}.")

    red_count = sum(1 for item in report.items if item.priority == "red")
    if red_count > MAX_RED_ITEMS:
        raise QuickAuditError("В AuditReport больше 3 red items.")

    return report


async def run_quick_text_audit(
    snapshot: MarketplaceCardSnapshot,
    facts: LocalAuditFacts,
) -> AuditReport:
    prompt = build_quick_text_audit_prompt(snapshot, facts)
    gemini_errors: list[str] = []

    for attempt in range(2):
        try:
            current_prompt = prompt if attempt == 0 else _retry_prompt(prompt)
            response = await call_gemini_quick_audit(current_prompt)
            return parse_quick_audit_response(response)
        except Exception as exc:
            gemini_errors.append(str(exc))
            logger.warning("Gemini quick audit attempt %d failed: %s", attempt + 1, exc)

    try:
        response = await call_openai_quick_audit(prompt)
        return parse_quick_audit_response(response)
    except Exception as exc:
        logger.warning("OpenAI quick audit fallback failed: %s", exc)
        joined_gemini_errors = "; ".join(error for error in gemini_errors if error)
        details = (
            f" Gemini: {joined_gemini_errors}."
            if joined_gemini_errors
            else ""
        )
        raise QuickAuditError(
            "Не удалось выполнить быстрый AI-аудит: Gemini не дал валидный "
            f"отчёт, OpenAI fallback тоже не сработал: {exc}.{details}"
        ) from exc
