"""WB and Ozon card analyzer using Playwright + stealth."""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT = Path(__file__).parent.parent / "auditor" / "data" / "card_analysis"
OUTPUT.mkdir(parents=True, exist_ok=True)

URLS = [
    ("wb", "https://www.wildberries.ru/catalog/277949363/detail.aspx"),
    ("ozon", "https://www.ozon.ru/product/1747453691/"),
]


async def analyze(page, platform: str, url: str):
    print(f"\n[{platform.upper()}] {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(5000)
    except Exception as e:
        print(f"  FAIL: {e}")
        return

    title = await page.title()
    print(f"  Title: {title[:100]}")

    body = await page.inner_text("body")
    lines = [l.strip() for l in body.split("\n") if l.strip()][:50]
    print(f"  Body lines: {len(lines)}")
    for i, line in enumerate(lines[:20]):
        print(f"    {i}: {line[:120]}")

    # Save screenshot
    path = OUTPUT / f"{platform}_playwright.png"
    await page.screenshot(path=str(path), full_page=True)
    print(f"  Screenshot: {path}")


async def main():
    async with async_playwright() as p:
        # Launch with stealthy args
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        # Remove navigator.webdriver flag
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()

        for platform, url in URLS:
            await analyze(page, platform, url)

        await browser.close()


asyncio.run(main())
