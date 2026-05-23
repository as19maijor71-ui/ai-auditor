"""Extract Ozon product data from share link page."""

import asyncio, re, json
import httpx
from urllib.parse import unquote


async def extract_ozon_product(share_url: str) -> dict:
    result = {"source": share_url}

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(share_url, headers={
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru",
        })

    html = r.text
    result["final_url"] = str(r.url)
    result["platform"] = "ozon"

    # Extract SKU from URL
    sku_match = re.search(r"/product/[^/]+-(\d+)/", str(r.url))
    if sku_match:
        result["sku"] = sku_match.group(1)

    # Method 1: LD+JSON structured data
    ld_pattern = r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>'
    ld_match = re.search(ld_pattern, html, re.DOTALL)
    if ld_match:
        try:
            ld_raw = ld_match.group(1)
            ld_data = json.loads(ld_raw)
            if isinstance(ld_data, list):
                ld_data = ld_data[0] if ld_data else {}

            result["name"] = ld_data.get("name", "")
            result["description"] = ld_data.get("description", "")[:2000]
            result["brand"] = ld_data.get("brand", "")
            result["sku"] = result.get("sku") or ld_data.get("sku", "")
            result["image"] = ld_data.get("image", "")

            offers = ld_data.get("offers", {})
            if isinstance(offers, dict):
                result["price"] = offers.get("price", "")
                result["currency"] = offers.get("priceCurrency", "")
                result["availability"] = offers.get("availability", "")

            agg_rating = ld_data.get("aggregateRating", {})
            if isinstance(agg_rating, dict):
                result["rating"] = agg_rating.get("ratingValue", "")
                result["reviews_count"] = agg_rating.get("reviewCount", "")

            result["ld_json_ok"] = True
        except Exception as e:
            result["ld_json_error"] = str(e)

    # Method 2: window.__NUXT__ state
    nuxt_match = re.search(
        r"window\.__NUXT__\s*=\s*\{[^}]*\};\s*window\.__NUXT__\.state\s*=\s*'([^']*)'",
        html, re.DOTALL
    )
    if nuxt_match:
        try:
            state_raw = nuxt_match.group(1)
            # Unescape unicode
            state_raw = state_raw.replace("\\u002F", "/").replace("\\u0026", "&")
            state_raw = state_raw.replace("\\u0022", '"').replace("\\n", "\n")
            state_raw = state_raw.replace("\\u003C", "<").replace("\\u003E", ">")
            state_raw = state_raw.replace("\\'", "'")
            nuxt_state = json.loads(state_raw)
            result["nuxt_ok"] = True

            # Look for product data in nuxt state
            def find_breadcrumb(obj, path=""):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k in ("product", "card", "sku", "item", "goods"):
                            if isinstance(v, dict):
                                return v, f"{path}.{k}"
                        found = find_breadcrumb(v, f"{path}.{k}")
                        if found:
                            return found
                return None

            product_data, product_path = find_breadcrumb(nuxt_state) or (None, "")
            if product_data:
                result["nuxt_product_path"] = product_path
                result["nuxt_product_keys"] = list(product_data.keys())[:20]

                # Enrich with Nuxt data
                result["name"] = result.get("name") or product_data.get("title") or product_data.get("name", "")
                result["description"] = result.get("description") or product_data.get("description", "") or ""
                result["price"] = result.get("price") or str(product_data.get("price", ""))
                result["characteristics"] = product_data.get("characteristics") or product_data.get("options") or []

        except Exception as e:
            result["nuxt_error"] = str(e)

    # Method 3: Meta tags
    for tag, key in [
        (r'<meta property="og:title" content="([^"]+)"', "og_title"),
        (r'<meta property="og:description" content="([^"]+)"', "og_description"),
        (r'<meta name="description" content="([^"]+)"', "meta_description"),
    ]:
        match = re.search(tag, html)
        if match:
            result[key] = match.group(1)

    # Build product text for audit
    parts = []
    if result.get("name"):
        parts.append(f"Название: {result['name']}")
    if result.get("brand"):
        parts.append(f"Бренд: {result['brand']}")
    if result.get("price"):
        parts.append(f"Цена: {result['price']} {result.get('currency', 'RUB')}")
    if result.get("rating"):
        parts.append(f"Рейтинг: {result['rating']} ({result.get('reviews_count', 0)} отзывов)")
    if result.get("description"):
        parts.append(f"Описание: {result['description'][:1000]}")
    if result.get("characteristics"):
        if isinstance(result["characteristics"], list):
            for ch in result["characteristics"]:
                if isinstance(ch, dict):
                    parts.append(f"{ch.get('name', '')}: {ch.get('value', '')}")
        elif isinstance(result["characteristics"], dict):
            for k, v in result["characteristics"].items():
                parts.append(f"{k}: {v}")

    result["product_text"] = "\n".join(parts)
    return result


async def main():
    share_url = "https://ozon.ru/t/gTGlaiy"
    data = await extract_ozon_product(share_url)

    print("=" * 60)
    print("OZON PRODUCT DATA FROM SHARE LINK")
    print("=" * 60)

    for key in ["name", "brand", "price", "currency", "sku", "rating", "reviews_count",
                 "availability", "description", "image", "final_url", "ld_json_ok",
                 "nuxt_ok", "nuxt_product_path", "nuxt_product_keys"]:
        val = data.get(key)
        if val is not None:
            if isinstance(val, str) and len(val) > 100:
                val = val[:100] + "..."
            print(f"  {key}: {val}")

    print(f"\n  product_text ({len(data.get('product_text', ''))} chars):")
    print(data.get("product_text", "")[:500])


asyncio.run(main())
