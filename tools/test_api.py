import asyncio
import httpx
import json

async def test_wb():
    nm = "277949363"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            f"https://card.wb.ru/cards/v2/detail",
            params={
                "appType": "1",
                "curr": "rub",
                "dest": "-1257786",
                "spp": "30",
                "lang": "ru",
                "nm": nm,
            },
        )
        print(f"WB API status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            products = data.get("data", {}).get("products", [])
            if products:
                p = products[0]
                name = p.get("name", "N/A")
                brand = p.get("brand", "N/A")
                sale_price = p.get("salePriceU", 0) / 100
                rating = p.get("reviewRating", "N/A")
                reviews = p.get("feedbacks", "N/A")
                pics_count = len(p.get("pics", []))
                desc = p.get("description", "N/A")
                print(f"Name: {name}")
                print(f"Brand: {brand}")
                print(f"Sale price: {sale_price} RUB")
                print(f"Rating: {rating}")
                print(f"Reviews: {reviews}")
                print(f"Photos: {pics_count}")
                print(f"Description (first 200): {desc[:200] if desc else 'N/A'}")
            else:
                print("No products found")
        else:
            print(f"Error: {r.text[:300]}")


async def test_ozon():
    url = "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/product/1747453691/"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        print(f"\nOzon API status: {r.status_code}")
        if r.status_code == 200:
            print(f"Keys: {list(r.json().keys())[:10]}")
        else:
            print(f"Error: {r.text[:300]}")


async def main():
    await test_wb()
    await test_ozon()

asyncio.run(main())
