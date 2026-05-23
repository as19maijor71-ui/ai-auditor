import asyncio
import httpx

async def test_wb_v2():
    """WB: different endpoints"""
    nm = "277949363"
    tests = [
        # Public card info endpoint
        f"https://basket-01.wbbasket.ru/vol0/data/{nm}/info/ru/card.json",
        f"https://basket-01.wbbasket.ru/vol1/data/{nm}/info/ru/card.json",
        f"https://basket-02.wbbasket.ru/vol2/data/{nm}/info/ru/card.json",
        f"https://basket-03.wbbasket.ru/vol3/data/{nm}/info/ru/card.json",
        # Ozon mobile-like API
        "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/product/1747453691/",
    ]
    for url in tests:
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.53 Mobile Safari/537.36",
                    "Accept": "*/*",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                }, follow_redirects=True)
                label = url.split("/")[-3] if "basket" in url else "ozon"
                print(f"{label} -> {r.status_code}: {r.text[:150]}")
        except Exception as e:
            print(f"{url[:60]} -> {e}")

async def main():
    await test_wb_v2()

asyncio.run(main())
