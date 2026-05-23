"""Fix Nuxt state extraction and get rating/reviews data."""

import asyncio, re, json
import httpx


async def main():
    url = "https://ozon.ru/t/gTGlaiy"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(url, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru",
        })

    html = r.text

    # Better Nuxt extraction
    nuxt_match = re.search(
        r"window\.__NUXT__\s*=\s*\{[^}]*\};\s*window\.__NUXT__\.state=['\"](.*?)['\"];</script>",
        html, re.DOTALL
    )
    if nuxt_match:
        raw = nuxt_match.group(1)
        # Handle the escaped JSON string
        # First, unescape the JSON string itself
        raw = raw.encode().decode("unicode_escape")
        try:
            state = json.loads(raw)
            print("Nuxt state loaded!")
            print(f"Top keys: {list(state.keys())[:10]}")
        except Exception as e:
            print(f"Nuxt parse error: {e}")
            # Try to find product data directly
            # Look for rating patterns
            rating_matches = re.findall(r'"rating[Vv]alue?"\s*:\s*([\d.]+)', raw)
            print(f"Rating values found: {rating_matches}")
            review_matches = re.findall(r'"review[Cc]ount"\s*:\s*(\d+)', raw)
            print(f"Review counts found: {review_matches}")

    # Also check for aggregateRating in the LD+JSON
    ld_raw = re.search(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    if ld_raw:
        ld = json.loads(ld_raw.group(1))
        if isinstance(ld, list):
            ld = ld[0]
        ar = ld.get("aggregateRating", {})
        print(f"\nLD JSON aggregateRating: {json.dumps(ar, indent=2)}")

        # Photos
        photos = ld.get("image", [])
        if isinstance(photos, str):
            photos = [photos]
        print(f"Photos: {len(photos)}")
        for p in photos[:3]:
            print(f"  {p}")


asyncio.run(main())
