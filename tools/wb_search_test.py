"""Find a working WB product and fetch its data."""

import asyncio
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        # Try to search for a product
        search_url = "https://www.wildberries.ru/catalog/0/search.aspx?search=платье"
        print(f"Loading search: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        title = await page.title()
        print(f"Title: {title}")

        # Get body text
        body = await page.inner_text("body")
        lines = [l.strip() for l in body.split("\n") if l.strip()]

        # Look for product names
        print("\nFirst 30 lines:")
        for i, line in enumerate(lines[:30]):
            print(f"  {i}: {line[:150]}")

        # Try to click first product
        links = await page.query_selector_all('a[href*="/catalog/"]')
        print(f"\nFound {len(links)} product links")
        for link in links[:3]:
            href = await link.get_attribute("href")
            text = (await link.inner_text()).strip()[:100]
            print(f"  {text} -> {href}")

        # Try clicking first product
        if links:
            await links[0].click()
            await page.wait_for_timeout(5000)
            print(f"\nAfter click - Title: {await page.title()}")
            body2 = await page.inner_text("body")
            lines2 = [l.strip() for l in body2.split("\n") if l.strip()]
            for i, line in enumerate(lines2[:30]):
                print(f"  {i}: {line[:150]}")

        await browser.close()


asyncio.run(main())
