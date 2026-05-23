"""Test if WB images are accessible via CDN."""
import asyncio
import httpx


async def main():
    nm = "277949363"
    # WB image URLs
    urls = []
    for vol in range(5):
        for part in range(5):
            urls.append(
                f"https://basket-{part:02d}.wbbasket.ru/vol{vol}/data/{nm}/images/big/1.webp"
            )

    urls.append(f"https://images.wbstatic.net/big/new/27790000/27794936-1.jpg")

    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
                r = await c.head(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.wildberries.ru/",
                })
                if r.status_code == 200:
                    ct = r.headers.get("content-type", "?")
                    cl = r.headers.get("content-length", "?")
                    print(f"OK 200 {url} ({ct}, {cl} bytes)")
                    break
                else:
                    print(f"{r.status_code} {url[:80]}")
        except Exception as e:
            print(f"ERR {url[:60]}: {e}")

    # Also try the main WB CDN
    main_url = "https://images.wbstatic.net/big/new/27790000/27794936-1.jpg"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.head(main_url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.wildberries.ru/",
            })
            print(f"\nMain CDN: {r.status_code} ({r.headers.get('content-type', '?')}, {r.headers.get('content-length', '?')}b)")
    except Exception as e:
        print(f"Main CDN ERR: {e}")


asyncio.run(main())
