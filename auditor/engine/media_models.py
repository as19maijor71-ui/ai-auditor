from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


MediaType = Literal[
    "main",
    "lifestyle",
    "infographic",
    "composition",
    "packaging",
    "review",
    "other",
]
MediaVerdict = Literal["keep", "remove", "move", "unknown"]
VideoType = Literal[
    "unboxing",
    "product_review",
    "product_usage",
    "before_after",
    "recipe_or_instruction",
    "customer_review",
    "slideshow",
    "ad",
    "weak_or_unclear",
    "other",
]


class MediaItem(BaseModel):
    position: int = Field(ge=1)
    ocr_text: str
    visual_summary: str
    media_type: MediaType
    preliminary_verdict: MediaVerdict

    @field_validator("ocr_text", "visual_summary")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


class VideoDescription(BaseModel):
    position: int | None = Field(default=None, ge=1)
    duration_seconds: int | None = Field(default=None, ge=0)
    shown_content: str
    video_type: VideoType
    quality_comment: str | None = None
    has_on_screen_text: bool | None = None
    has_call_to_action: bool | None = None

    @field_validator("shown_content")
    @classmethod
    def _shown_content_must_not_be_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("shown_content must not be empty")
        return stripped

    @field_validator("quality_comment")
    @classmethod
    def _empty_quality_comment_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None
