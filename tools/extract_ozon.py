"""Extract product data from Ozon share link page."""

import asyncio, re, json
import httpx


async def fetch_ozon_share(url: str) -> dict:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru",
        })
        print(f"Status: {r.status_code}, URL: {r.url}, Size: {len(r.text)}")

        result = {"url": str(r.url), "html_fetched": True, "status": r.status_code}

        # Method 1: LD+JSON structured data
        ld_match = re.search(
            r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>',
            r.text, re.DOTALL
        )
        if ld_match:
            try:
                data = json.loads(ld_match.group(1))
                if isinstance(data, list):
                    data = data[0] if data else {}
                result["name"] = data.get("name", "")
                result["description"] = data.get("description", "")[:1000]
                offers = data.get("offers", {})
                result["price"] = offers.get("price", "") if isinstance(offers, dict) else ""
                result["currency"] = offers.get("priceCurrency", "") if isinstance(offers, dict) else ""
                result["image"] = data.get("image", "")
                result["ld_json_found"] = True
                print(f"  LD JSON: name={result['name'][:60]}")
            except Exception as e:
                print(f"  LD JSON error: {e}")

        # Method 2: __NEXT_DATA__ (NextJS)
        next_match = re.search(
            r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            r.text, re.DOTALL
        )
        if next_match:
            try:
                nd = json.loads(next_match.group(1))
                props = nd.get("props", {}).get("pageProps", {})
                result["next_data_found"] = True
                result["pageProps_keys"] = list(props.keys())[:15]
                # Look deep for product
                if "product" in props:
                    p = props["product"]
                    if isinstance(p, dict):
                        result["name"] = result.get("name") or p.get("name", "")
                        result["description"] = result.get("description") or p.get("description", "")
                        result["price"] = result.get("price") or str(p.get("price", ""))
                        result["product_keys"] = list(p.keys())[:15]
                        print(f"  NextJS product found: keys={result['product_keys']}")
            except Exception as e:
                print(f"  NextJS error: {e}")

        # Method 3: Meta tags
        for tag, key in [
            (r'<meta\s+property="og:title"\s+content="([^"]+)"', "og_title"),
            (r'<meta\s+property="og:description"\s+content="([^"]+)"', "og_desc"),
            (r'<meta\s+property="og:image"\s+content="([^"]+)"', "og_image"),
            (r'<meta\s+property="product:price:amount"\s+content="([^"]+)"', "meta_price"),
            (r'<meta\s+name="description"\s+content="([^"]+)"', "meta_desc"),
        ]:
            match = re.search(tag, r.text)
            if match:
                result[key] = match.group(1)
                print(f"  Meta: {key}={match.group(1)[:80]}")

        # Method 4: window.__data or __NUXT__
        for pattern, name in [
            (r'window\.__NUXT__\s*=\s*(.*?);\s*</script>', "nuxt"),
            (r'window\.__PRELOADED_STATE__\s*=\s*(.*?});', "preloaded"),
            (r'__APP_STATE__\s*=\s*({.*?});', "app_state"),
            (r'window\.__INITIAL_STATE__\s*=\s*({.*?});', "initial_state"),
        ]:
            match = re.search(pattern, r.text, re.DOTALL)
            if match:
                try:
                    state = json.loads(match.group(1))
                    result[f"{name}_found"] = True
                    result[f"{name}_keys"] = list(state.keys())[:15] if isinstance(state, dict) else "not dict"
                    print(f"  State: {name} keys={result[f'{name}_keys']}")
                except Exception:
                    result[f"{name}_found"] = False

        # Method 5: Full text extraction (last resort)
        from html.parser import HTMLParser

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text = []
                self.skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "noscript"):
                    self.skip = True

            def handle_endtag(self, tag):
                if tag in ("script", "style", "noscript"):
                    self.skip = False

            def handle_data(self, data):
                if not self.skip:
                    t = data.strip()
                    if t:
                        self.text.append(t)

        extractor = TextExtractor()
        extractor.feed(r.text)
        result["visible_text_lines"] = len(extractor.text)
        result["visible_text"] = "\n".join(extractor.text[:100])

        return result


async def main():
    url = "https://ozon.ru/t/gTGlaiy"
    data = await fetch_ozon_share(url)
    print(f"\n{'='*60}")
    print("FINAL RESULT:")
    for k, v in data.items():
        if isinstance(v, str) and len(v) > 200:
            v = v[:200] + "..."
        elif isinstance(v, list) and len(v) > 10:
            v = v[:10]
        print(f"  {k}: {v}")


asyncio.run(main())
