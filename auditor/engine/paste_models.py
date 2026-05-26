from typing import Literal

from pydantic import BaseModel, Field


class CompetitorCard(BaseModel):
    name: str
    price: int | None = None
    old_price: int | None = None
    rating: float | None = None
    review_count: int | None = None
    position: int


class MarketplaceCardSnapshot(BaseModel):
    platform: Literal["wb", "ozon", "unknown"]
    source_type: Literal["paste", "txt_file"]
    raw_text: str = Field(exclude=True, repr=False)
    cleaned_text: str = Field(exclude=True, repr=False)
    product_name: str | None = None
    brand: str | None = None
    sku: str | None = None
    category_path: list[str] = Field(default_factory=list)
    current_price: int | None = None
    old_price: int | None = None
    unit_price: str | None = None
    rating: float | None = None
    review_count: int | None = None
    question_count: int | None = None
    seller_name: str | None = None
    variants: list[str] = Field(default_factory=list)
    characteristics: dict[str, str] = Field(default_factory=dict)
    description: str | None = None
    review_fragments: list[str] = Field(default_factory=list)
    competitor_cards: list[CompetitorCard] = Field(default_factory=list)
    missing_blocks: list[str] = Field(default_factory=list)


class LocalAuditFacts(BaseModel):
    title_length: int | None = None
    repeated_words: dict[str, int] = Field(default_factory=dict)
    has_description: bool
    description_length: int
    characteristics_count: int
    has_price: bool
    has_rating: bool
    has_reviews: bool
    competitors_count: int
    min_competitor_price: int | None = None
    avg_competitor_price: int | None = None
