"""Parse WB and Ozon seller export files (XLSX/CSV)."""

import csv
import io
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ExportedProduct:
    platform: str
    row: int
    sku: str
    title: str
    price: str
    brand: str
    description: str
    category: str
    old_price: str
    barcode: str
    images: str


class ExportParseError(Exception):
    pass


def _normalize_header(h: str) -> str:
    return h.strip().lower().replace("\n", " ").replace("  ", " ")


def _detect_platform(headers: list[str]) -> str | None:
    norm = [_normalize_header(h) for h in headers]
    norm_set = set(norm)

    wb_markers = {"артикул продавца", "артикул wb", "баркод", "название товара"}
    ozon_markers = {"артикул", "ozon id", "название товара", "описание"}

    if len(wb_markers & norm_set) >= 3:
        return "wb"
    if len(ozon_markers & norm_set) >= 3:
        return "ozon"
    return None


def _parse_xlsx(file_bytes: bytes, sheet_name: str = "") -> tuple[list[str], list[list[str]]]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb[sheet_name] if sheet_name else wb.active
    if not ws:
        raise ExportParseError("Не удалось прочитать лист Excel")

    rows_raw: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        rows_raw.append([str(c) if c is not None else "" for c in row])

    if not rows_raw:
        raise ExportParseError("Файл пустой")

    headers = rows_raw[0]
    data_rows = rows_raw[1:]
    return headers, data_rows


def _parse_csv(file_bytes: bytes) -> tuple[list[str], list[list[str]]]:
    text = file_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows_raw = list(reader)
    if not rows_raw:
        raise ExportParseError("Файл пустой")
    headers = rows_raw[0]
    data_rows = rows_raw[1:]
    return headers, data_rows


def _extract_products(headers: list[str], data: list[list[str]], platform: str) -> list[ExportedProduct]:
    norm_headers = [_normalize_header(h) for h in headers]
    products: list[ExportedProduct] = []

    for row_idx, row in enumerate(data):
        if not any(c.strip() for c in row):
            continue
        values = dict(zip(norm_headers, row))

        if platform == "wb":
            sku = values.get("артикул продавца", "") or values.get("vendorcode", "")
            title = values.get("название товара", "") or values.get("название", "") or values.get("name", "")
            price = values.get("цена", "")
            brand = values.get("бренд", "") or values.get("brand", "")
            description = ""  # WB export does not include description
            category = values.get("категория", "") or values.get("subject", "")
            old_price = values.get("цена до скидки", "") or values.get("oldprice", "")
            barcode = values.get("баркод", "") or values.get("barcode", "")
            images = values.get("медиафайлы", "") or values.get("mediafiles", "")
        else:
            sku = values.get("артикул", "") or values.get("offer_id", "")
            title = values.get("название товара", "") or values.get("название", "") or values.get("name", "")
            price = values.get("цена", "") or values.get("price", "")
            brand = values.get("бренд", "") or values.get("vendor", "")
            description = values.get("описание", "") or values.get("description", "")
            category = ""
            old_price = values.get("цена до скидки", "") or values.get("old_price", "")
            barcode = values.get("штрихкод", "") or values.get("barcode", "")
            images = values.get("ссылка на главное фото", "") or values.get("ссылки на фото", "") or values.get("images", "")

        if not title.strip():
            continue

        products.append(ExportedProduct(
            platform=platform,
            row=row_idx + 2,
            sku=sku.strip(),
            title=title.strip(),
            price=price.strip(),
            brand=brand.strip(),
            description=description.strip(),
            category=category.strip(),
            old_price=old_price.strip(),
            barcode=barcode.strip(),
            images=images.strip(),
        ))

    return products


def parse_export_file(file_bytes: bytes, filename: str) -> list[ExportedProduct]:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext in ("xlsx", "xls"):
        headers, data = _parse_xlsx(file_bytes)
    elif ext == "csv":
        headers, data = _parse_csv(file_bytes)
    else:
        raise ExportParseError(f"Неподдерживаемый формат: .{ext}. Загрузите XLSX или CSV.")

    platform = _detect_platform(headers)
    if not platform:
        raise ExportParseError(
            "Не удалось определить платформу (WB/Ozon) по заголовкам.\n"
            "Убедитесь, что загружаете файл экспорта из личного кабинета продавца."
        )

    products = _extract_products(headers, data, platform)
    if not products:
        raise ExportParseError("В файле не найдено товаров с названиями.")

    platform_name = "Wildberries" if platform == "wb" else "Ozon"
    logger.info("Parsed %d products from %s export %s", len(products), platform_name, filename)
    return products


def product_to_text(p: ExportedProduct) -> str:
    parts: list[str] = []

    parts.append(f"Название: {p.title}")
    if p.brand:
        parts.append(f"Бренд: {p.brand}")
    if p.price:
        price_str = f"Цена: {p.price} ₽"
        if p.old_price:
            price_str += f" (старая: {p.old_price} ₽)"
        parts.append(price_str)
    if p.category:
        parts.append(f"Категория: {p.category}")
    if p.barcode:
        parts.append(f"Артикул/Штрихкод: {p.barcode}")
    if p.description:
        parts.append(f"Описание:\n{p.description}")

    return "\n\n".join(parts)
