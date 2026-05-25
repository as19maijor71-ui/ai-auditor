import base64
import json
import logging
import re

import httpx
from pydantic import BaseModel, Field

from auditor.config import settings

logger = logging.getLogger(__name__)


class AuditItem(BaseModel):
    section: str
    priority: str
    finding: str
    recommendation: str
    why: str


class AuditReport(BaseModel):
    url: str = ""
    platform: str = ""
    product_name: str = ""
    overall_score: int = 0
    items: list[AuditItem] = Field(default_factory=list)
    summary: str = ""
    competitor_insight: str = ""

    raw_response: str = ""


def _extract_json(text: str) -> str:
    json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if json_match:
        return json_match.group(1).strip()
    brace_start = text.find("{")
    if brace_start == -1:
        return text
    depth = 0
    for i, ch in enumerate(text[brace_start:], start=brace_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start : i + 1]
    return text[brace_start:]


def _strict_json_instruction(strict: bool) -> str:
    if not strict:
        return ""
    return "\n\nВозвращай ТОЛЬКО валидный JSON. Без пояснений, без markdown. Только JSON."


async def call_openrouter(prompt: str, max_tokens: int, strict_json: bool = False) -> str:
    async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT) as client:
        request_body: dict = {
            "model": settings.OPENROUTER_MODEL,
            "messages": [
                {"role": "user", "content": prompt + _strict_json_instruction(strict_json)}
            ],
            "max_tokens": max_tokens,
        }
        if strict_json:
            request_body["response_format"] = {"type": "json_object"}

        response = await client.post(
            settings.OPENROUTER_BASE_URL,
            headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json=request_body,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


async def call_yandexgpt(prompt: str, max_tokens: int, strict_json: bool = False) -> str:
    async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT) as client:
        response = await client.post(
            settings.YANDEXGPT_BASE_URL,
            headers={
                "Authorization": f"Api-Key {settings.YANDEXGPT_API_KEY}",
                "x-folder-id": settings.YANDEXGPT_FOLDER_ID,
                "Content-Type": "application/json",
            },
            json={
                "modelUri": f"gpt://{settings.YANDEXGPT_FOLDER_ID}/yandexgpt/latest",
                "completionOptions": {
                    "stream": False,
                    "temperature": 0.7,
                    "maxTokens": max_tokens,
                },
                "messages": [
                    {"role": "user", "text": prompt + _strict_json_instruction(strict_json)}
                ],
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["result"]["alternatives"][0]["message"]["text"]


async def call_ai(prompt: str, max_tokens: int = 4096, strict_json: bool = False) -> str:
    logger.info("Calling AI, provider=%s, model=%s", settings.AI_PROVIDER, settings.OPENROUTER_MODEL)

    if settings.AI_PROVIDER == "openrouter":
        try:
            return await call_openrouter(prompt, max_tokens, strict_json)
        except Exception as e:
            logger.warning(f"OpenRouter failed: {e}")
            if settings.YANDEXGPT_API_KEY:
                logger.info("Falling back to YandexGPT")
                return await call_yandexgpt(prompt, max_tokens, strict_json)
            raise

    if settings.AI_PROVIDER == "yandexgpt":
        return await call_yandexgpt(prompt, max_tokens, strict_json)

    raise ValueError(f"Unknown AI provider: {settings.AI_PROVIDER}")


async def call_vision(image_data: bytes, prompt: str) -> str:
    # Compress large images to avoid OpenRouter limits
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(image_data))
        if max(img.size) > 1024:
            img.thumbnail((1024, 1024), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=75)
            image_data = buf.getvalue()
    except Exception:
        pass

    image_b64 = base64.b64encode(image_data).decode("utf-8")

    async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT) as client:
        response = await client.post(
            settings.OPENROUTER_BASE_URL,
            headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.VISION_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_b64}"
                                },
                            },
                        ]
                    }
                ],
                "max_tokens": 4096,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


def parse_audit_response(response: str) -> AuditReport:
    json_str = _extract_json(response)
    try:
        report = AuditReport.model_validate_json(json_str)
        if report.raw_response:
            return report
        report.raw_response = response
        return report
    except Exception:
        pass

    try:
        data = json.loads(json_str)
        report = AuditReport(
            url=data.get("url", ""),
            platform=data.get("platform", ""),
            product_name=data.get("product_name", ""),
            overall_score=data.get("overall_score", 0),
            items=[AuditItem(**item) for item in data.get("items", [])],
            summary=data.get("summary", ""),
            competitor_insight=data.get("competitor_insight", ""),
            raw_response=response,
        )
        return report
    except Exception:
        pass

    return AuditReport(
        summary=response,
        overall_score=0,
        raw_response=response,
    )


async def audit_card(product_text: str, url: str, platform: str) -> AuditReport:
    from auditor.templates.prompts import build_audit_prompt

    prompt = build_audit_prompt(product_text, platform, url)

    try:
        response = await call_ai(prompt, max_tokens=4096, strict_json=True)
        return parse_audit_response(response)
    except Exception as e:
        logger.error(f"Audit AI call failed: {e}")
        return AuditReport(
            url=url,
            platform=platform,
            summary=f"Не удалось выполнить аудит: {e}",
            raw_response=str(e),
        )
