"""Анализатор структуры карточек WB и Ozon.

Открывает карточки через Playwright, делает скриншоты секций,
извлекает текст и строит карту данных.
"""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

OUTPUT_DIR = Path(__file__).parent.parent / "auditor" / "data" / "card_analysis"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

WB_URLS = [
    "https://www.wildberries.ru/catalog/277949363/detail.aspx",
    "https://www.wildberries.ru/catalog/290567891/detail.aspx",
    "https://www.wildberries.ru/catalog/312456789/detail.aspx",
    "https://www.wildberries.ru/catalog/256789123/detail.aspx",
    "https://www.wildberries.ru/catalog/198765432/detail.aspx",
]

OZON_URLS = [
    "https://www.ozon.ru/product/1747453691/",
    "https://www.ozon.ru/product/1854321987/",
    "https://www.ozon.ru/product/1654389271/",
    "https://www.ozon.ru/product/1928374650/",
    "https://www.ozon.ru/product/1567894321/",
]


async def analyze_card(page, url: str, platform: str, index: int) -> dict | None:
    print(f"\n{'='*60}")
    print(f"[{platform.upper()}] Карточка {index + 1}: {url}")
    print("=" * 60)

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        print(f"  ❌ Не загрузилась: {e}")
        return None

    title = await page.title()
    print(f"  Title: {title}")

    if "Antibot" in title or "Доступ ограничен" in title:
        print(f"  ❌ Антибот-защита")
        return None

    # Скриншот всей страницы
    screenshot_path = OUTPUT_DIR / f"{platform}_{index + 1}_full.png"
    await page.screenshot(path=str(screenshot_path), full_page=True)
    print(f"  📸 Скриншот: {screenshot_path}")

    # Извлекаем видимый текст
    body_text = await page.inner_text("body")
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]
    text_path = OUTPUT_DIR / f"{platform}_{index + 1}_text.txt"
    text_path.write_text("\n".join(lines), encoding="utf-8")

    # Ищем ключевые элементы
    sections = {
        "title": None,
        "price": None,
        "rating": None,
        "photos_count": 0,
        "description": None,
        "characteristics": None,
        "reviews": None,
    }

    # Заголовок
    for selector in ["h1", "[data-wba-header-name]", '[class*="title"]', '[class*="name"]']:
        try:
            el = await page.query_selector(selector)
            if el:
                sections["title"] = (await el.inner_text()).strip()[:200]
                break
        except Exception:
            pass

    # Цена
    for selector in ['[class*="price"]', '[data-wba-price]', 'span:has-text("₽")']:
        try:
            el = await page.query_selector(selector)
            if el:
                text = await el.inner_text()
                if "₽" in text:
                    sections["price"] = text.strip()[:50]
                    break
        except Exception:
            pass

    # Рейтинг
    try:
        rating_el = await page.query_selector('[class*="rating"], [class*="star"], [class*="review"]')
        if rating_el:
            sections["rating"] = (await rating_el.inner_text()).strip()[:50]
    except Exception:
        pass

    # Фото — считаем изображения товара
    try:
        imgs = await page.query_selector_all('img[class*="photo"], img[class*="gallery"], img[class*="slide"]')
        sections["photos_count"] = len(imgs) if imgs else "не удалось посчитать"
    except Exception:
        sections["photos_count"] = "не удалось посчитать"

    # Описание
    try:
        desc_el = await page.query_selector(
            '[class*="description"], [class*="desc"], [class*="text"], [data-wba-description]'
        )
        if desc_el:
            sections["description"] = (await desc_el.inner_text()).strip()[:500]
    except Exception:
        pass

    # Характеристики
    try:
        chars_el = await page.query_selector(
            '[class*="characteristics"], [class*="chars"], [class*="options"], [class*="params"], table'
        )
        if chars_el:
            sections["characteristics"] = (await chars_el.inner_text()).strip()[:500]
    except Exception:
        pass

    # Отзывы
    try:
        rev_el = await page.query_selector('[class*="review"], [class*="feedback"], [class*="comment"]')
        if rev_el:
            sections["reviews"] = (await rev_el.inner_text()).strip()[:500]
    except Exception:
        pass

    print(f"  📊 Найдено:")
    for key, val in sections.items():
        if val:
            display = str(val)[:80] + ("..." if len(str(val)) > 80 else "")
            print(f"     {key}: {display}")
        else:
            print(f"     {key}: ❌ не найдено")

    return sections


async def main():
    results = {"wb": [], "ozon": []}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        page = await context.new_page()

        for i, url in enumerate(WB_URLS):
            result = await analyze_card(page, url, "wb", i)
            if result:
                results["wb"].append({"url": url, "sections": result})

        for i, url in enumerate(OZON_URLS):
            result = await analyze_card(page, url, "ozon", i)
            if result:
                results["ozon"].append({"url": url, "sections": result})

        await browser.close()

    # Сохраняем результаты
    report_path = OUTPUT_DIR / "analysis_report.json"
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"📊 ИТОГИ АНАЛИЗА")
    print(f"{'='*60}")
    print(f"WB: проанализировано {len(results['wb'])} карточек")
    print(f"Ozon: проанализировано {len(results['ozon'])} карточек")
    print(f"Отчёт: {report_path}")
    print(f"Скриншоты: {OUTPUT_DIR}")

    # Сводка: какие секции найдены
    print(f"\n--- СВОДКА ПО СЕКЦИЯМ ---")
    for platform, cards in results.items():
        print(f"\n{platform.upper()}:")
        if not cards:
            print("  Нет данных")
            continue
        section_stats = {}
        for card in cards:
            for key, val in card["sections"].items():
                if val:
                    section_stats[key] = section_stats.get(key, 0) + 1
        total = len(cards)
        for key, count in section_stats.items():
            pct = count * 100 // total
            print(f"  {key}: {count}/{total} ({pct}%)")


if __name__ == "__main__":
    asyncio.run(main())
