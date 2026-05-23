import asyncio, re
from auditor.engine.url_fetcher import fetch_product_page

async def main():
    url = "https://ozon.ru/t/gTGlaiy"
    html = await fetch_product_page(url)
    print(f"HTML length: {len(html)}")

    # Search for ld+json in the full HTML
    if "ld+json" in html.lower():
        pos = html.lower().index("ld+json")
        print(f"ld+json found at pos {pos}: ...{html[pos-30:pos+80]}...")
    else:
        print("ld+json NOT in HTML")

    # Try all regex variations
    variations = [
        ('<script type="application/ld+json">', 'script type="application/ld+json"'),
        ("<script type='application/ld+json'>", "script type='application/ld+json'"),
        ("type=\"application/ld+json\"", "double quote"),
        ("type='application/ld+json'", "single quote"),
    ]
    for pattern, name in variations:
        if pattern in html:
            print(f"FOUND: {name}")
            idx = html.index(pattern)
            end_idx = html.index("</script>", idx)
            print(f"  Content ({end_idx - idx - len(pattern)} chars):")
            snippet = html[idx:end_idx + 9]
            print(f"  {snippet[:300]}...")
            break


asyncio.run(main())
