import asyncio, re, json
import httpx

async def main():
    url = "https://ozon.ru/t/gTGlaiy"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru",
        })
        print(f"Status: {r.status_code}")
        print(f"Final URL: {r.url}")
        print(f"Content length: {len(r.text)}")

        # LD JSON
        ld_match = re.search(
            r'<script type="application/ld\+json">(.*?)</script>', r.text, re.DOTALL
        )
        if ld_match:
            try:
                data = json.loads(ld_match.group(1))
                if isinstance(data, list):
                    data = data[0]
                name = data.get("name", "N/A")
                desc = data.get("description", "N/A")
                offers = data.get("offers", {})
                price = offers.get("price", "N/A") if offers else "N/A"
                print(f"\nName: {name}")
                print(f"Price: {price}")
                print(f"Description: {desc[:200]}")
            except Exception as e:
                print(f"LD parse error: {e}")

        # Title
        title_match = re.search(r"<title>(.*?)</title>", r.text)
        if title_match:
            print(f"\nTitle: {title_match.group(1)}")

        # Antibot check
        if any(phrase in r.text for phrase in ["Antibot", "abt-challenge", "Доступ ограничен"]):
            print("\nBLOCKED: Antibot detected")
        else:
            print("\nOK: Page loaded")

        # Extract product info from NextJS data
        next_match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL
        )
        if next_match:
            nd = json.loads(next_match.group(1))
            props = nd.get("props", {}).get("pageProps", {})
            print(f"\nNextJS pageProps keys: {list(props.keys())[:20]}")
            # Look for product data
            for key in ["product", "catalog", "item", "sku", "card", "detail"]:
                if key in props:
                    val = props[key]
                    if isinstance(val, dict):
                        print(f"  {key} keys: {list(val.keys())[:15]}")
                    elif isinstance(val, str):
                        print(f"  {key}: {val[:100]}")


asyncio.run(main())
