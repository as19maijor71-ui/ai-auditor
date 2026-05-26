from auditor.bot.paste_collector import (
    append_paste_part,
    build_paste_status_text,
    decode_txt_file,
    is_enough_for_quick_audit,
    is_txt_document,
)


def test_is_txt_document_accepts_txt_case_insensitive() -> None:
    assert is_txt_document("card.txt") is True
    assert is_txt_document("CARD.TXT") is True


def test_is_txt_document_rejects_other_formats() -> None:
    assert is_txt_document("card.xlsx") is False
    assert is_txt_document("card.csv") is False
    assert is_txt_document(None) is False


def test_decode_txt_file_reads_utf8_sig() -> None:
    assert decode_txt_file("Название товара".encode("utf-8-sig")) == "Название товара"


def test_decode_txt_file_replaces_invalid_utf8() -> None:
    assert "�" in decode_txt_file(b"\xff\xfe\xfa")


def test_append_paste_part_joins_parts_with_newline() -> None:
    text, truncated = append_paste_part("Первая часть", "Вторая часть")

    assert text == "Первая часть\nВторая часть"
    assert truncated is False


def test_append_paste_part_truncates_at_limit() -> None:
    text, truncated = append_paste_part("a" * 29_999, "bbb")

    assert len(text) == 30_000
    assert truncated is True


def test_is_enough_for_quick_audit_rejects_short_text() -> None:
    assert is_enough_for_quick_audit("x" * 299) is False


def test_is_enough_for_quick_audit_accepts_300_chars() -> None:
    assert is_enough_for_quick_audit("x" * 300) is True


def test_build_paste_status_text_contains_counts() -> None:
    status = build_paste_status_text(total_chars=1234, parts_count=3, truncated=False)

    assert "3" in status
    assert "1234" in status
