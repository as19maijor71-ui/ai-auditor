from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Literal

import pytest

from auditor.engine.paste_parser import (
    MAX_PASTE_RAW_CHARS,
    build_local_audit_facts,
    detect_paste_platform,
    normalize_paste_text,
    parse_marketplace_paste,
    sanitize_personal_data,
)
from auditor.engine.url_fetcher import (
    CompetitorFetchError,
    detect_platform as detect_url_platform,
    fetch_product_page,
)

FixtureCase = tuple[str, Literal["wb", "ozon"]]

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "paste"
FIXTURES: list[FixtureCase] = [
    ("wb_01_dress_clothing.txt", "wb"),
    ("wb_02_coffee_food.txt", "wb"),
    ("wb_03_humidifier_electronics.txt", "wb"),
    ("wb_04_face_cream_beauty.txt", "wb"),
    ("wb_05_dog_bed_pets.txt", "wb"),
    ("ozon_01_headphones_electronics.txt", "ozon"),
    ("ozon_02_matcha_food.txt", "ozon"),
    ("ozon_03_pan_home.txt", "ozon"),
    ("ozon_04_baby_diapers.txt", "ozon"),
    ("ozon_05_serum_beauty.txt", "ozon"),
]


def _read_fixture(filename: str) -> str:
    return (FIXTURE_DIR / filename).read_text(encoding="utf-8")


@pytest.mark.parametrize(("filename", "expected_platform"), FIXTURES)
def test_parse_each_paste_fixture(filename: str, expected_platform: str) -> None:
    snapshot = parse_marketplace_paste(_read_fixture(filename))

    assert snapshot.platform == expected_platform
    assert snapshot.product_name is not None
    assert snapshot.product_name.strip()
    assert snapshot.competitor_cards
    assert snapshot.product_name != snapshot.competitor_cards[0].name


def test_fixture_quality_gate_counts() -> None:
    snapshots = [
        parse_marketplace_paste(_read_fixture(filename))
        for filename, _expected_platform in FIXTURES
    ]

    platform_and_name_count = sum(
        snapshot.platform != "unknown" and snapshot.product_name is not None
        for snapshot in snapshots
    )
    signal_count = sum(
        snapshot.current_price is not None
        or snapshot.rating is not None
        or snapshot.review_count is not None
        for snapshot in snapshots
    )
    separated_count = sum(
        snapshot.product_name != snapshot.competitor_cards[0].name
        for snapshot in snapshots
        if snapshot.product_name is not None and snapshot.competitor_cards
    )

    assert platform_and_name_count == 10
    assert signal_count >= 8
    assert separated_count == 10


def test_unknown_garbage_text_does_not_raise() -> None:
    snapshot = parse_marketplace_paste("абракадабра\nне карточка\n123\n***")

    assert snapshot.platform == "unknown"
    assert snapshot.product_name is None
    assert snapshot.current_price is None
    assert snapshot.rating is None
    assert snapshot.review_count is None


def test_recommendation_block_before_description_does_not_swallow_main_sections() -> None:
    snapshot = parse_marketplace_paste(
        """Ozon
Название товара: Тестовый товар
Цена: 1000 ₽
Подобрали для вас
Конкурент первый
900 ₽
Описание
Важное описание основного товара после виджета
Характеристики
Материал: хлопок"""
    )

    assert snapshot.description == "Важное описание основного товара после виджета"
    assert snapshot.characteristics == {"Материал": "хлопок"}
    assert [card.name for card in snapshot.competitor_cards] == ["Конкурент первый"]


def test_competitor_review_count_does_not_include_rating_tail() -> None:
    snapshot = parse_marketplace_paste(
        """Ozon
Название товара: Тестовый товар
Цена: 1000 ₽
Подобрали для вас
Конкурент хороший 1 490 ₽ 4.7 120 отзывов"""
    )

    assert snapshot.competitor_cards[0].rating == 4.7
    assert snapshot.competitor_cards[0].review_count == 120


def test_unit_price_is_not_category_path() -> None:
    snapshot = parse_marketplace_paste(
        """Ozon
Название товара: Матча 100 г
Цена: 1 250 ₽
Цена за 100 г: 1 250 ₽ / 100 г
4,8 684 отзыва"""
    )

    assert snapshot.unit_price == "1 250 ₽ / 100 г"
    assert snapshot.category_path == []


def test_invalid_runtime_source_type_falls_back_to_paste() -> None:
    snapshot = parse_marketplace_paste(
        """Wildberries
Название товара: Тестовый товар
Цена: 1000 ₽""",
        source_type="file",  # type: ignore[arg-type]
    )

    assert snapshot.source_type == "paste"
    assert snapshot.product_name == "Тестовый товар"


def test_delivery_address_is_removed_from_cleaned_text() -> None:
    snapshot = parse_marketplace_paste(
        """Ozon
Город доставки: Москва
Адрес доставки: ул. Ленина, 10, кв. 5
Название товара: Тестовый товар
Цена: 1000 ₽"""
    )

    assert "ул. Ленина" not in snapshot.raw_text
    assert "ул. Ленина" not in snapshot.cleaned_text


def test_common_personal_data_patterns_are_removed() -> None:
    snapshot = parse_marketplace_paste(
        """Ozon
Название товара: Тестовый товар
Цена: 1000 ₽
buyer@example.com
+7 999 123-45-67
Адрес: Москва, ул. Пушкина, 1
Получателю: Иван Иванов"""
    )

    assert "buyer@example.com" not in snapshot.cleaned_text
    assert "+7 999 123-45-67" not in snapshot.cleaned_text
    assert "ул. Пушкина" not in snapshot.cleaned_text
    assert "Иван Иванов" not in snapshot.cleaned_text


def test_raw_and_cleaned_text_are_excluded_from_model_dump() -> None:
    snapshot = parse_marketplace_paste(
        """Ozon
Название товара: Тестовый товар
Цена: 1000 ₽"""
    )

    dumped = snapshot.model_dump()

    assert "raw_text" not in dumped
    assert "cleaned_text" not in dumped


def test_large_paste_input_is_truncated_before_parsing() -> None:
    raw_text = (
        "Ozon\n"
        "Название товара: Тестовый товар\n"
        "Цена: 1000 ₽\n"
        + ("Описание очень длинное\n" * 4000)
    )
    snapshot = parse_marketplace_paste(raw_text)

    assert len(snapshot.raw_text) <= MAX_PASTE_RAW_CHARS
    assert "input_truncated" in snapshot.missing_blocks


def test_sanitize_personal_data_preserves_product_like_words() -> None:
    text = "Получатель радиосигнала беспроводной USB\nТелефон получателя: +7 999 123-45-67"

    sanitized = sanitize_personal_data(text)

    assert "Получатель радиосигнала" in sanitized
    assert "Телефон получателя" not in sanitized


def test_url_platform_detection_rejects_lookalike_hosts() -> None:
    assert detect_url_platform("https://ozon.ru.evil.test/product/test/") is None
    assert detect_url_platform("https://wildberries.ru.evil.test/catalog/123/detail.aspx") is None


def test_fetch_product_page_rejects_lookalike_hosts_before_network() -> None:
    with pytest.raises(CompetitorFetchError, match="Неподдерживаемая платформа"):
        asyncio.run(fetch_product_page("https://ozon.ru.evil.test/product/test/"))


def test_build_local_audit_facts_for_wb() -> None:
    snapshot = parse_marketplace_paste(_read_fixture("wb_01_dress_clothing.txt"))
    facts = build_local_audit_facts(snapshot)

    assert facts.title_length == len(snapshot.product_name or "")
    assert facts.has_description is True
    assert facts.description_length > 0
    assert facts.characteristics_count >= 3
    assert facts.has_price is True
    assert facts.has_rating is True
    assert facts.has_reviews is True
    assert facts.competitors_count >= 2
    assert facts.min_competitor_price is not None
    assert facts.avg_competitor_price is not None


def test_build_local_audit_facts_for_ozon() -> None:
    snapshot = parse_marketplace_paste(_read_fixture("ozon_01_headphones_electronics.txt"))
    facts = build_local_audit_facts(snapshot)

    assert facts.title_length == len(snapshot.product_name or "")
    assert facts.has_description is True
    assert facts.description_length > 0
    assert facts.characteristics_count >= 3
    assert facts.has_price is True
    assert facts.has_rating is True
    assert facts.has_reviews is True
    assert facts.competitors_count >= 2
    assert facts.min_competitor_price is not None
    assert facts.avg_competitor_price is not None


def test_public_functions_have_type_annotations() -> None:
    functions = [
        normalize_paste_text,
        detect_paste_platform,
        parse_marketplace_paste,
        build_local_audit_facts,
    ]

    for func in functions:
        signature = inspect.signature(func)
        assert signature.return_annotation is not inspect.Signature.empty
        for parameter in signature.parameters.values():
            assert parameter.annotation is not inspect.Parameter.empty
