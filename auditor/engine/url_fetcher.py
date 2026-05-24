import json
import logging
import re
from urllib.parse import urlparse

import httpx

from auditor.config import settings

logger = logging.getLogger(__name__)

class CompetitorFetchError(Exception):
    pass


_WB_CATALOG_RE = re.compile(r"^/catalog/(\d+)/detail\.aspx")
_OZON_PATH_RE = re.compile(r"^/product/")
_OZON_SHARE_RE = re.compile(r"^/t/")

# WB internal JSON API — no Cloudflare, no blocking.
_WB_API = "https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&spp=30&ab_testing=false&nm={nm}"


def detect_platform(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    hostname = parsed.hostname or ""
    hostname = hostname.lower().removeprefix("www.").removeprefix("m.")

    if hostname == "wildberries.ru" and _WB_CATALOG_RE.match(parsed.path):
        return "wb"
    if hostname == "ozon.ru" and (_OZON_PATH_RE.match(parsed.path) or _OZON_SHARE_RE.match(parsed.path)):
        return "ozon"
    return None


def _extract_wb_nm(url: str) -> str | None:
    match = _WB_CATALOG_RE.search(url)
    if match:
        return match.group(1)
    return None


async def _fetch_wb_json(nm: str) -> str:
    """Fetch WB product data via public JSON API — always works, no blocking."""
    url = _WB_API.format(nm=nm)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Language": "ru",
    }
    client_kwargs: dict = {"timeout": settings.COMPETITOR_FETCH_TIMEOUT}
    if settings.PROXY_URL:
        client_kwargs["proxies"] = settings.PROXY_URL
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.get(url, headers=headers)
    if response.status_code == 404:
        raise CompetitorFetchError("Карточка не найдена")
    response.raise_for_status()
    return response.text


async def _fetch_ozon_page(url: str) -> str:
    """Fetch Ozon page with mobile user-agent. May return 403 from datacenter IPs."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.5 Mobile/15E148 Safari/604.1"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Referer": "https://www.google.com/",
    }
    client_kwargs: dict = {"timeout": settings.COMPETITOR_FETCH_TIMEOUT}
    if settings.PROXY_URL:
        client_kwargs["proxies"] = settings.PROXY_URL
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
    if response.status_code == 404:
        raise CompetitorFetchError("Карточка не найдена")
    if response.status_code in (403, 429, 498):
        raise CompetitorFetchError("Ozon заблокировал загрузку. Отправь текст карточки вручную.")
    response.raise_for_status()
    return response.text


async def fetch_product_page(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()

    if "wildberries.ru" in hostname:
        nm = _extract_wb_nm(url)
        if not nm:
            raise CompetitorFetchError("Не удалось определить артикул WB из ссылки")
        return await _fetch_wb_json(nm)

    if "ozon.ru" in hostname:
        return await _fetch_ozon_page(url)

    raise CompetitorFetchError("Неподдерживаемая платформа")


_LD_JSON_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)


def extract_product_text(raw_data: str, platform: str) -> str:
    try:
        if platform == "wb":
            return _extract_wb_text(raw_data)
        elif platform == "ozon":
            return _extract_ozon_text(raw_data)
        else:
            return ""
    except Exception as e:
        logger.warning(f"Failed to extract product text for {platform}: {e}")
        return ""


def _extract_wb_text(json_str: str) -> str:
    """Extract product info from WB JSON API response."""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return ""

    products = data.get("data", {}).get("products", [])
    if not products:
        return ""

    product = products[0]
    parts: list[str] = []

    name = product.get("name") or ""
    if name:
        parts.append(f"Название: {name}")

    brand = product.get("brand") or ""
    if brand:
        parts.append(f"Бренд: {brand}")

    sizes = product.get("sizes", [])
    if sizes:
        price_kop = sizes[0].get("price", {}).get("product", 0)
        if price_kop:
            parts.append(f"Цена: {price_kop / 100:.0f} ₽")

    rating = product.get("reviewRating") or product.get("rating")
    feedbacks = product.get("feedbacks")
    if rating is not None:
        fb_str = f" ({feedbacks} отзывов)" if feedbacks else ""
        parts.append(f"Рейтинг: {rating}{fb_str}")

    description = product.get("description") or ""
    if description and description.strip():
        parts.append(f"Описание:\n{description}")

    options = product.get("options") or []
    if isinstance(options, list) and options:
        chars_lines = _flatten_characteristics(options)
        if chars_lines:
            parts.append(f"Характеристики:\n{chars_lines}")

    text = "\n\n".join(parts)
    return text[:settings.COMPETITOR_MAX_LENGTH]


def _extract_ozon_text(html: str) -> str:
    match = _LD_JSON_RE.search(html)
    if not match:
        return ""

    raw = match.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""

    if isinstance(data, list):
        data = data[0] if data else {}

    parts: list[str] = []

    name = data.get("name") or ""
    if name:
        parts.append(f"Название: {name}")

    brand = data.get("brand") or ""
    if brand:
        parts.append(f"Бренд: {brand}")

    offers = data.get("offers", {})
    if isinstance(offers, dict):
        price = offers.get("price", "")
        currency = offers.get("priceCurrency", "")
        if price:
            parts.append(f"Цена: {price} {currency}")

    agg_rating = data.get("aggregateRating", {})
    if isinstance(agg_rating, dict):
        rating = agg_rating.get("ratingValue", "")
        reviews = agg_rating.get("reviewCount", "")
        if rating:
            parts.append(f"Рейтинг: {rating} ({reviews} отзывов)")

    description = data.get("description") or ""
    if description:
        parts.append(f"Описание:\n{description}")

    sku = data.get("sku") or ""
    if sku:
        parts.append(f"Артикул: {sku}")

    text = "\n\n".join(parts)
    return text[:settings.COMPETITOR_MAX_LENGTH]


def _flatten_characteristics(chars: list) -> str:
    lines: list[str] = []
    for item in chars:
        if isinstance(item, dict):
            name = item.get("name") or item.get("title") or ""
            value = item.get("value") or item.get("text") or ""
            if name and value:
                lines.append(f"{name}: {value}")
    return "\n".join(lines)
