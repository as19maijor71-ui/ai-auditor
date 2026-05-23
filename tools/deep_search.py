import asyncio, re, json
import httpx


async def main():
    url = "https://ozon.ru/t/gTGlaiy"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(url, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru",
        })

    html = r.text
    print(f"HTML size: {len(html)}")

    # Find all script tags with content > 100 chars
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    print(f"\nScript tags with content: {len(scripts)}")

    for i, script in enumerate(scripts):
        script = script.strip()
        if len(script) > 50:
            preview = script[:200].replace("\n", " ")
            print(f"\n  #{i}: {len(script)} chars - {preview}")

    # Search for JSON-like structures
    for keyword in ["product", "price", "offers", "rating", "name", "description"]:
        count = html.lower().count(f'"{keyword}"')
        if count > 0:
            print(f'\n"{keyword}": {count} occurrences')

    # Try to find any JSON blobs
    json_braces = re.findall(r"\{[^{}]*\"name\"\s*:\s*\"[^\"]+\"[^{}]*\}", html)
    if json_braces:
        print(f"\nJSON with name field: {len(json_braces)}")
        for j in json_braces[:3]:
            print(f"  {j[:200]}")

    # Check for meta tags
    for tag in ["og:title", "og:description", "og:image", "product:price:amount"]:
        match = re.search(rf'<meta\s+property="{tag}"\s+content="([^"]+)"', html)
        if match:
            print(f"Meta {tag}: {match.group(1)[:100]}")


asyncio.run(main())
