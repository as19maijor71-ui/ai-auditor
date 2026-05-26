MAX_PASTE_CHARS: int = 30_000
MIN_QUICK_AUDIT_CHARS: int = 300


def is_txt_document(filename: str | None) -> bool:
    if filename is None:
        return False
    return filename.strip().lower().endswith(".txt")


def decode_txt_file(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def append_paste_part(
    current_text: str,
    new_part: str,
    max_chars: int = MAX_PASTE_CHARS,
) -> tuple[str, bool]:
    if len(current_text) >= max_chars:
        return current_text[:max_chars], True

    combined = f"{current_text}\n{new_part}" if current_text else new_part
    if len(combined) <= max_chars:
        return combined, False
    return combined[:max_chars], True


def build_paste_status_text(total_chars: int, parts_count: int, truncated: bool) -> str:
    text = (
        "✅ Часть карточки принята.\n\n"
        f"Частей принято: {parts_count}\n"
        f"Символов накоплено: {total_chars}\n\n"
    )
    if truncated:
        text += "⚠️ Лимит 30 000 символов достигнут. Лишний текст обрезан.\n\n"
    text += "Когда всё вставил — нажми «✅ Запустить быстрый аудит»."
    return text


def is_enough_for_quick_audit(text: str) -> bool:
    return len(text.strip()) >= MIN_QUICK_AUDIT_CHARS
