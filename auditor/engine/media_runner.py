from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable

import httpx

from auditor.config import settings
from auditor.engine.generator import AuditItem
from auditor.engine.media_models import MediaItem, VideoDescription
from auditor.templates.prompts import build_media_audit_prompt


class MediaAuditError(RuntimeError):
    """Raised when media analysis cannot produce a valid structured result."""


def _decode_json_object_prefix(text: str) -> str | None:
    try:
        parsed, end_index = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return text[:end_index].strip()


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


def _extract_gemini_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MediaAuditError("Gemini вернул ответ без текста медиа-аудита.") from exc

    text_parts = [
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and part.get("text")
    ]
    if not text_parts:
        raise MediaAuditError("Gemini вернул пустой текст медиа-аудита.")
    return "\n".join(text_parts)


def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        raise MediaAuditError(f"Gemini media API вернул HTTP {status_code}.") from exc


async def call_gemini_media_audit(image_data: bytes, position: int) -> MediaItem:
    if not settings.GEMINI_API_KEY:
        raise MediaAuditError("GEMINI_API_KEY не настроен.")
    if len(image_data) > settings.MEDIA_MAX_IMAGE_BYTES:
        raise MediaAuditError("Изображение больше допустимого лимита.")

    image_b64 = base64.b64encode(image_data).decode("utf-8")
    prompt = build_media_audit_prompt(position)
    url = (
        f"{settings.GEMINI_BASE_URL.rstrip('/')}/"
        f"{settings.GEMINI_VISION_MODEL}:generateContent"
    )
    request_body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": "image/jpeg",
                            "data": image_b64,
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
        },
    }

    async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT) as client:
        response = await client.post(
            url,
            params={"key": settings.GEMINI_API_KEY},
            json=request_body,
        )
        _raise_for_status(response)
        return parse_media_item_response(_extract_gemini_text(response.json()), position)


def parse_media_item_response(response: str, position: int) -> MediaItem:
    json_text = _extract_json_object(response)
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise MediaAuditError("Gemini вернул невалидный JSON MediaItem.") from exc

    if not isinstance(payload, dict):
        raise MediaAuditError("Gemini вернул JSON не в формате объекта MediaItem.")

    payload["position"] = position
    try:
        return MediaItem.model_validate(payload)
    except Exception as exc:
        raise MediaAuditError("Gemini вернул невалидный MediaItem.") from exc


def parse_video_description(text: str) -> VideoDescription:
    fields = _extract_video_fields(text)
    shown_content = fields.get("shown_content", "").strip()
    if not shown_content:
        raise MediaAuditError("Заполни поле «Что показано» в описании видео.")

    try:
        return VideoDescription(
            position=_parse_optional_int(fields.get("position", "")),
            duration_seconds=_parse_duration_seconds(fields.get("duration_seconds", "")),
            shown_content=shown_content,
            video_type=_parse_video_type(fields.get("video_type", "")),
            quality_comment=_empty_to_none(fields.get("quality_comment", "")),
            has_on_screen_text=_parse_bool(fields.get("has_on_screen_text", "")),
            has_call_to_action=_parse_bool(fields.get("has_call_to_action", "")),
        )
    except Exception as exc:
        if isinstance(exc, MediaAuditError):
            raise
        raise MediaAuditError("Описание видео не удалось разобрать.") from exc


def build_media_audit_items(
    media_items: list[MediaItem],
    videos: list[VideoDescription],
) -> list[AuditItem]:
    audit_items: list[AuditItem] = []

    for item in sorted(media_items, key=lambda media: media.position):
        audit_items.append(_build_photo_audit_item(item))

    for index, video in enumerate(
        sorted(videos, key=lambda item: item.position if item.position is not None else 10_000),
        start=1,
    ):
        audit_items.append(_build_video_audit_item(video, index))

    return audit_items


def _extract_video_fields(text: str) -> dict[str, str]:
    label_map = {
        "позиция в галерее": "position",
        "позиция": "position",
        "длительность": "duration_seconds",
        "что показано": "shown_content",
        "тип видео": "video_type",
        "тип": "video_type",
        "качество": "quality_comment",
        "есть ли текст на экране": "has_on_screen_text",
        "текст на экране": "has_on_screen_text",
        "есть ли призыв к покупке": "has_call_to_action",
        "призыв к покупке": "has_call_to_action",
    }
    fields: dict[str, str] = {}
    current_key = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        key, value = _split_labeled_line(line, label_map)
        if key:
            current_key = key
            fields[current_key] = _append_field_value(fields.get(current_key, ""), value)
            continue

        if current_key:
            fields[current_key] = _append_field_value(fields.get(current_key, ""), line)

    return fields


def _split_labeled_line(line: str, label_map: dict[str, str]) -> tuple[str, str]:
    normalized_line = _normalize_text(line)
    for label, key in sorted(label_map.items(), key=lambda item: len(item[0]), reverse=True):
        normalized_label = _normalize_text(label)
        if normalized_line == normalized_label:
            return key, ""
        if normalized_line.startswith(f"{normalized_label}:"):
            value = line.split(":", 1)[1].strip() if ":" in line else ""
            return key, value
    return "", ""


def _append_field_value(current: str, value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return current
    return f"{current}\n{stripped}".strip() if current else stripped


def _parse_optional_int(text: str) -> int | None:
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None


def _parse_duration_seconds(text: str) -> int | None:
    normalized = _normalize_text(text)
    if not normalized:
        return None

    colon_match = re.search(r"(?:(\d+):)?(\d{1,2}):(\d{2})", normalized)
    if colon_match:
        hours = int(colon_match.group(1) or 0)
        minutes = int(colon_match.group(2))
        seconds = int(colon_match.group(3))
        return hours * 3600 + minutes * 60 + seconds

    short_colon_match = re.search(r"\b(\d{1,2}):(\d{2})\b", normalized)
    if short_colon_match:
        return int(short_colon_match.group(1)) * 60 + int(short_colon_match.group(2))

    minutes = 0
    seconds = 0
    minute_match = re.search(r"(\d+)\s*(?:мин|minute)", normalized)
    if minute_match:
        minutes = int(minute_match.group(1))
    second_match = re.search(r"(\d+)\s*(?:сек|second|s\b)", normalized)
    if second_match:
        seconds = int(second_match.group(1))
    if minute_match or second_match:
        return minutes * 60 + seconds

    number_match = re.search(r"\d+", normalized)
    return int(number_match.group(0)) if number_match else None


def _parse_bool(text: str) -> bool | None:
    normalized = _normalize_text(text)
    if not normalized:
        return None
    yes_markers = ("да", "есть", "true", "yes", "y", "+")
    no_markers = ("нет", "отсутств", "false", "no", "n", "-")
    if any(marker in normalized for marker in yes_markers):
        return True
    if any(marker in normalized for marker in no_markers):
        return False
    return None


def _parse_video_type(text: str) -> str:
    normalized = _normalize_text(text)
    checks: tuple[tuple[Iterable[str], str], ...] = (
        (("распаков", "unboxing"), "unboxing"),
        (("до/после", "до после", "before after"), "before_after"),
        (("рецепт", "инструкц", "способ"), "recipe_or_instruction"),
        (("отзыв", "customer"), "customer_review"),
        (("слайд", "slideshow"), "slideshow"),
        (("реклам", "ad"), "ad"),
        (("слаб", "непонят", "неясн", "weak"), "weak_or_unclear"),
        (("использ", "примен", "usage"), "product_usage"),
        (("обзор", "review"), "product_review"),
    )
    for markers, video_type in checks:
        if any(marker in normalized for marker in markers):
            return video_type
    return "other"


def _empty_to_none(text: str) -> str | None:
    stripped = text.strip()
    return stripped or None


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("ё", "е").strip().lower())


def _build_photo_audit_item(item: MediaItem) -> AuditItem:
    type_label = _media_type_label(item.media_type)
    verdict_label = _media_verdict_label(item.preliminary_verdict)
    priority = {
        "remove": "red",
        "move": "yellow",
        "unknown": "yellow",
        "keep": "green",
    }[item.preliminary_verdict]

    ocr_part = f" Видимый текст: {_truncate(item.ocr_text, 220)}" if item.ocr_text else ""
    return AuditItem(
        section="media",
        priority=priority,
        finding=(
            f"Фото {item.position}: тип — {type_label}; предварительный вердикт — "
            f"{verdict_label}. {_truncate(item.visual_summary, 260)}{ocr_part}"
        ),
        recommendation=_photo_recommendation(item),
        why="Фото и порядок галереи влияют на первое понимание товара и доверие к карточке.",
    )


def _build_video_audit_item(video: VideoDescription, index: int) -> AuditItem:
    type_label = _video_type_label(video.video_type)
    position = f"позиция {video.position}" if video.position is not None else "позиция не указана"
    duration = (
        f", длительность {video.duration_seconds} сек."
        if video.duration_seconds is not None
        else ""
    )
    priority = "red" if video.video_type == "weak_or_unclear" else "green"
    if video.has_call_to_action is False or video.has_on_screen_text is False:
        priority = "yellow" if priority != "red" else priority

    return AuditItem(
        section="media",
        priority=priority,
        finding=(
            f"Видео {index}: {position}{duration} Тип — {type_label}. "
            f"Показано: {_truncate(video.shown_content, 260)}"
        ),
        recommendation=_video_recommendation(video),
        why="Видео усиливает доверие, если быстро показывает товар, сценарий использования и понятный следующий шаг.",
    )


def _photo_recommendation(item: MediaItem) -> str:
    if item.preliminary_verdict == "keep":
        return f"Оставить фото {item.position}, если оно не дублирует соседние слайды."
    if item.preliminary_verdict == "remove":
        return f"Заменить или убрать фото {item.position}: текущий слайд выглядит слабым для галереи."
    if item.preliminary_verdict == "move":
        return f"Переставить фото {item.position} ближе к логичному месту в галерее по его типу."
    return f"Проверить фото {item.position} вручную перед финальным решением."


def _video_recommendation(video: VideoDescription) -> str:
    parts: list[str] = []
    if video.video_type == "weak_or_unclear":
        parts.append("Переснять видео: показать товар крупно, сценарий использования и результат.")
    else:
        parts.append("Оставить видео в галерее, если оно не дублирует фото и быстро раскрывает пользу товара.")
    if video.position is None or video.position > 5:
        parts.append("Поставить видео ближе к началу галереи, обычно после главного фото или первой инфографики.")
    if video.has_on_screen_text is False:
        parts.append("Добавить короткий читаемый текст на экране с 1-2 ключевыми выгодами.")
    if video.has_call_to_action is False:
        parts.append("Добавить мягкий призыв к покупке или выбору варианта в финале.")
    if video.quality_comment:
        parts.append(f"Учесть качество: {_truncate(video.quality_comment, 160)}")
    return " ".join(parts)


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


def _truncate(text: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", text.strip())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"
