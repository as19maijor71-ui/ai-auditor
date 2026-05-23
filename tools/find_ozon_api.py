"""Find Ozon API endpoint for product data."""

import asyncio
import httpx


async def test_ozon_api(url: str, sku: str):
    apis = [
        f"https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=/product/{sku}/",
        f"https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/product/{sku}/",
        f"https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=/product/{sku}/?layout_container=pdpPage2column&layout_page_index=1",
        f"https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=/product/{sku}/?from=share_web",
    ]
    for api_url in apis:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(api_url, headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
                    "Accept": "application/json",
                    "Accept-Language": "ru",
                    "Referer": url,
                })
                print(f"\n{api_url[:80]}...")
                print(f"  Status: {r.status_code}")
                if r.status_code == 200:
                    import json
                    try:
                        data = r.json()
                        keys = list(data.keys())[:10] if isinstance(data, dict) else "list"
                        print(f"  Keys: {keys}")
                        if "widgetStates" in data or "layout" in data:
                            print("  SUCCESS: Found product layout data!")
                    except Exception as e:
                        print(f"  Not JSON: {e}")
                elif r.status_code == 307:
                    print(f"  Redirect to: {r.headers.get('Location', 'N/A')}")
                else:
                    print(f"  Body: {r.text[:200]}")
        except Exception as e:
            print(f"  Error: {e}")


async def test_wb_share():
    """Test WB share URL format"""
    wb_urls = [
        "https://www.wildberries.ru/catalog/277949363/detail.aspx",
        "https://wb.ru/product/277949363",
    ]
    for url in wb_urls:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ru",
                })
                print(f"\nWB: {url}")
                print(f"  Status: {r.status_code}, Size: {len(r.text)}")
                import re
                title = re.search(r"<title>(.*?)</title>", r.text)
                if title:
                    print(f"  Title: {title.group(1)}")
        except Exception as e:
            print(f"WB {url}: {e}")


async def main():
    sku = "667351366"
    url = f"https://www.ozon.ru/product/chay-loshad-simvol-goda-2026-chernyy-listovoy-podarochnyy-get-joy-kitay-50-gr-v-zhestyanoy-shkatulke-{sku}/"
    await test_ozon_api(url, sku)
    await test_wb_share()


asyncio.run(main())
