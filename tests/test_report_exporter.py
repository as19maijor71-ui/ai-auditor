from __future__ import annotations

import re

from pydantic import BaseModel

from auditor.engine import report_exporter
from auditor.engine.generator import AuditItem, AuditReport
from auditor.engine.report_exporter import (
    build_report_filename,
    export_audit_report_text,
    extract_top_actions,
)


def _sample_report(product_name: str = "Кофе в зернах / Brazil 1 кг") -> AuditReport:
    return AuditReport(
        url="manual_input",
        platform="ozon",
        product_name=product_name,
        overall_score=72,
        summary="Нужно усилить первые блоки карточки без обещаний точного роста.",
        competitor_insight="Конкуренты из копипаста дешевле на 100 ₽.",
        items=[
            AuditItem(
                section="title",
                priority="red",
                finding="Заголовок перегружен повторами ключей.",
                recommendation="Сократить заголовок до читаемой формулы товар + бренд + ключевое свойство.",
                why="Покупателю проще понять товар, а маркетплейсу — релевантность.",
            ),
            AuditItem(
                section="price_competitors",
                priority="yellow",
                finding="Цена выше части конкурентной полки.",
                recommendation="Проверить цену рядом с ближайшими конкурентами и объяснить отличие в карточке.",
                why="Без объяснения отличия цена выглядит слабее в выдаче.",
            ),
            AuditItem(
                section="description",
                priority="yellow",
                finding="Описание не раскрывает сценарии использования.",
                recommendation="Добавить 3-5 практичных сценариев использования товара.",
                why="Сценарии помогают покупателю быстрее принять решение.",
            ),
            AuditItem(
                section="seo",
                priority="green",
                finding="SEO-блок выглядит базово заполненным.",
                recommendation="Добавить недостающие LSI-слова без переспама.",
                why="Это расширяет релевантность без потери читаемости.",
            ),
            AuditItem(
                section="reviews_risks",
                priority="green",
                finding="Отзывы и риски в копипасте ограничены.",
                recommendation="Проверить частые возражения покупателей в отзывах.",
                why="Возражения лучше закрывать в описании и инфографике.",
            ),
            AuditItem(
                section="characteristics",
                priority="green",
                finding="Характеристики частично заполнены.",
                recommendation="Сверить обязательные характеристики категории.",
                why="Характеристики участвуют в фильтрах и выборе товара.",
            ),
        ],
    )


def test_export_contains_header_score_product_and_platform() -> None:
    text = export_audit_report_text(_sample_report())

    assert "AI-АУДИТ КАРТОЧКИ WB/OZON" in text
    assert "Кофе в зернах / Brazil 1 кг" in text
    assert "Площадка: Ozon" in text
    assert "Общий балл: 72/100" in text


def test_export_contains_three_top_actions() -> None:
    report = _sample_report()

    actions = extract_top_actions(report)
    text = export_audit_report_text(report)

    assert len(actions) == 3
    for index, action in enumerate(actions, start=1):
        assert f"{index}. {action}" in text


def test_extract_top_actions_fills_three_actions_for_sparse_report() -> None:
    report = AuditReport(platform="wb", product_name="Тест")

    actions = extract_top_actions(report)

    assert len(actions) == 3


def test_export_always_contains_unchecked_block() -> None:
    text = export_audit_report_text(_sample_report())

    assert "Что не проверено" in text


def test_media_not_added_marks_media_as_unchecked() -> None:
    text = export_audit_report_text(_sample_report(), media_added=False).lower()

    assert "фото" in text
    assert "инфографика" in text
    assert "видео" in text
    assert "порядок галереи" in text


def test_exporter_neutralizes_exact_growth_promises() -> None:
    report = _sample_report()
    report.items[0].recommendation = "Это даст +30% продаж и CTR +20%."
    report.summary = "Гарантированно поднимет ранжирование на 5 позиций."

    text = export_audit_report_text(report)

    assert "+30%" not in text
    assert "+20%" not in text
    assert "Гарантированно поднимет" not in text


def test_filename_is_safe_ascii_txt_and_limited() -> None:
    report = _sample_report("Кофе: Brazil / 100% лучший <товар> " * 5)

    filename = build_report_filename(report)

    assert filename.endswith(".txt")
    assert len(filename) <= 80
    assert re.fullmatch(r"[A-Za-z0-9_-]+\.txt", filename)


def test_empty_product_name_uses_default_filename() -> None:
    assert build_report_filename(_sample_report("")) == "audit_report.txt"


def test_price_competitors_section_goes_to_price_block() -> None:
    text = export_audit_report_text(_sample_report())
    price_block = text.split("Цена и конкурентная полка", 1)[1].split("\n\n", 1)[0]

    assert "Цена выше части конкурентной полки" in price_block
    assert "Конкуренты из копипаста дешевле" in price_block


def test_reviews_risks_section_goes_to_reviews_block() -> None:
    text = export_audit_report_text(_sample_report())
    reviews_block = text.split("Отзывы и риски", 1)[1].split("\n\n", 1)[0]

    assert "Отзывы и риски в копипасте ограничены" in reviews_block
    assert "Проверить частые возражения покупателей" in reviews_block


def test_exporter_does_not_define_new_dto() -> None:
    dto_classes = [
        value
        for value in vars(report_exporter).values()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == report_exporter.__name__
    ]

    assert dto_classes == []
