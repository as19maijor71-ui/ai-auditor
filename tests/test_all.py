"""Automated tests for AI Auditor — run on server."""
import sys, os, json
import asyncio

sys.path.insert(0, ".")

TMP = os.environ.get("TEMP", os.environ.get("TMPDIR", "/tmp"))

TESTS_PASSED = 0
TESTS_FAILED = 0

def ok(name):
    global TESTS_PASSED
    TESTS_PASSED += 1
    print(f"  ✅ {name}")

def fail(name, err=""):
    global TESTS_FAILED
    TESTS_FAILED += 1
    print(f"  ❌ {name}: {err}")

# ─── Test 1: Config ───
print("\n=== 1. Config ===")
from auditor.config import settings
assert settings.BOT_TOKEN, "BOT_TOKEN missing"
assert settings.OPENROUTER_API_KEY, "OPENROUTER_API_KEY missing"
ok("Config loaded")
ok(f"ADMIN_USER_ID={settings.ADMIN_USER_ID}")
ok(f"FREE_AUDIT_LIMIT={settings.FREE_AUDIT_LIMIT}")

# ─── Test 2: Excel parser ───
print("\n=== 2. Excel Parser ===")
import openpyxl

# Create test WB file
wb = openpyxl.Workbook()
ws = wb.active
ws.append(["Артикул продавца", "Артикул WB", "Баркод", "Бренд", "Название товара", "Категория", "Цена", "Цена до скидки"])
ws.append(["SKU-001", "240151303", "4601234567890", "TestBrand", "Платье женское летнее хлопковое", "Платья", "2499", "4999"])
ws.append(["SKU-002", "240151304", "4601234567891", "TestBrand", "Футболка мужская оверсайз", "Футболки", "999", "1999"])
wb.save(os.path.join(TMP, "test_wb.xlsx"))

from auditor.engine.excel_parser import parse_export_file, product_to_text, ExportedProduct

products = parse_export_file(open(os.path.join(TMP, "test_wb.xlsx"), "rb").read(), "test.xlsx")
assert len(products) == 2, f"expected 2, got {len(products)}"
ok(f"WB: {len(products)} products parsed")
ok(f"  Product 1: {products[0].title[:40]}")
ok(f"  Platform: {products[0].platform}")

assert products[0].platform == "wb", f"expected wb, got {products[0].platform}"
ok("Platform detection: WB")

# Test Ozon detection
wb2 = openpyxl.Workbook()
ws2 = wb2.active
ws2.append(["Артикул", "Ozon ID", "Название товара", "Цена", "Цена до скидки", "Описание", "Бренд"])
ws2.append(["OZ-001", "667351366", "Чай подарочный", "499", "799", "Премиальный чай в шкатулке", "Get Joy"])
wb2.save(os.path.join(TMP, "test_ozon.xlsx"))

products = parse_export_file(open(os.path.join(TMP, "test_ozon.xlsx"), "rb").read(), "test.xlsx")
assert len(products) == 1
ok(f"Ozon: {len(products)} product parsed")
assert products[0].platform == "ozon", f"expected ozon, got {products[0].platform}"
ok("Platform detection: Ozon")

text = product_to_text(products[0])
assert "Чай подарочный" in text
assert "Get Joy" in text
ok(f"product_to_text OK ({len(text)} chars)")

# ─── Test 3: Cleaner ───
print("\n=== 3. Cleaner ===")
from auditor.engine.cleaner import clean_wb_text

test_text = """Платье женское летнее
2499 ₽
Рекомендуем также
Юбка карандаш 1500 ₽
Вам помог этот отзыв?
Да 0
Нет 0
Наведите камеру и скачайте
Об Ozon Контакты
Подборки товаров в категории"""

result = clean_wb_text(test_text)
assert "Платье женское летнее" in result, "Product title missing"
ok("Product title preserved")
assert "Рекомендуем также" not in result, "Recommendations not removed"
ok("Recommendations removed")
assert "Наведите камеру" not in result, "Footer not removed"
ok("Footer removed")
assert "Да 0" not in result, "Review votes not removed"
ok("Review votes removed")
assert "Подборки товаров" not in result, "Category links not removed"
ok("Category links removed")

# Test large Ozon dump with reviews + recommendations
big_text = """О товаре
Тип Кофе в зернах
Бренд initio de coffee

Описание
Происхождение и качество
Кофе Бразилия Сантос — это легендарный сорт арабики.

Рекомендуем также
Кофе в зернах 1 кг BELLO COFFEE 1243 ₽
Кофе в зернах Tasty Coffee 1933 ₽

Все отзывы
Наталья Б. 24 мая 2026 Прекрасный кофе
Вам помог этот отзыв? Да 0 Нет 0

Наведите камеру и скачайте бесплатное приложение Ozon
Об Ozon Контакты"""

result2 = clean_wb_text(big_text)
assert "Кофе Бразилия Сантос" in result2, "Coffee description missing"
ok("Coffee description preserved")
assert "BELLO COFFEE" not in result2, "Recommended products not removed"
ok("Recommended products block removed")
assert "Наталья Б" not in result2, "Review author not removed"
ok("Review block removed")
assert "Наведите камеру" not in result2, "Footer not removed (large)"
ok("Footer removed (large)")

# ─── Test 4: Storage ───
print("\n=== 4. Storage ===")
import sqlite3
conn = sqlite3.connect(os.path.join(TMP, "test_audit.db"))

# Set up tables
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, approved_by INTEGER, created_at TEXT)")
conn.execute("CREATE TABLE IF NOT EXISTS user_usage (user_id INTEGER PRIMARY KEY, free_audits_used INTEGER, updated_at TEXT)")
conn.execute("CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, url TEXT, platform TEXT, score INTEGER, created_at TEXT)")

# Add admin to whitelist
conn.execute("INSERT OR IGNORE INTO whitelist VALUES (1385057376, 'admin', 'Admin', 1385057376, datetime('now'))")
conn.commit()

# Check whitelist
row = conn.execute("SELECT 1 FROM whitelist WHERE user_id=1385057376").fetchone()
assert row is not None
ok("Admin in whitelist")

# Check usage
conn.execute("INSERT OR IGNORE INTO user_usage VALUES (1385057376, 0, datetime('now'))")
conn.execute("INSERT OR IGNORE INTO user_usage VALUES (999999999, 5, datetime('now'))")
conn.commit()

row = conn.execute("SELECT free_audits_used FROM user_usage WHERE user_id=1385057376").fetchone()
assert row[0] == 0, f"Expected 0, got {row[0]}"
ok(f"Admin usage: {row[0]}")

# Test limit check logic (replicate _check_audit_limit)
whitelisted = conn.execute("SELECT 1 FROM whitelist WHERE user_id=1385057376").fetchone() is not None
if whitelisted:
    ok("Admin bypass: whitelisted → no limit")
else:
    row = conn.execute("SELECT free_audits_used FROM user_usage WHERE user_id=1385057376").fetchone()
    used = row[0] if row else 0
    if used < 3:
        ok(f"Admin bypass: under limit ({used}/3)")
    else:
        fail("Admin should be unlimited", f"used {used}/3")

# Test non-whitelisted user with exceeded limit
non_whitelisted = conn.execute("SELECT 1 FROM whitelist WHERE user_id=999999999").fetchone() is not None
row = conn.execute("SELECT free_audits_used FROM user_usage WHERE user_id=999999999").fetchone()
used = row[0] if row else 0
if not non_whitelisted and used >= 3:
    ok(f"Non-whitelist limit enforced: {used}/3 → blocked")
else:
    fail("Non-whitelist should be blocked", f"used={used}, whitelisted={non_whitelisted}")

conn.close()
os.remove(os.path.join(TMP, "test_audit.db"))

# ─── Test 5: Audit prompt ───
print("\n=== 5. Audit Prompt ===")
from auditor.templates.prompts import build_audit_prompt

prompt = build_audit_prompt("Тестовый товар: Платье красное, цена 5000", "wb", "https://test.ru")
assert "Платье красное" in prompt
assert "wb" in prompt
ok(f"Prompt built ({len(prompt)} chars)")
assert "title" in prompt.lower()
ok("Prompt contains audit blocks")

# ─── Test 6: URL fetcher ───
print("\n=== 6. URL Fetcher ===")
from auditor.engine.url_fetcher import detect_platform

assert detect_platform("https://www.wildberries.ru/catalog/240151303/detail.aspx") == "wb"
ok("WB URL detection")
assert detect_platform("https://www.ozon.ru/product/chay-123/") == "ozon"
ok("Ozon URL detection")
assert detect_platform("https://www.ozon.ru/t/abc123") == "ozon"
ok("Ozon share URL detection")
assert detect_platform("https://google.com") is None
ok("Non-marketplace URL rejected")

# Test extract_product_text with mock data
from auditor.engine.url_fetcher import extract_product_text

# WB HTML mock with __NEXT_DATA__
wb_html = '''<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"product":{"name":"Тестовый товар WB","brand":"TestBrand","description":"Отличный товар","characteristics":[{"name":"Цвет","value":"Красный"}]}}}}</script>'''
wb_text = extract_product_text(wb_html, "wb")
assert "Тестовый товар WB" in wb_text
ok("WB text extraction from HTML")

# Ozon HTML mock with LD+JSON
ozon_html = '''<script type="application/ld+json">{"name":"Тестовый товар Ozon","description":"Премиум товар","offers":{"price":"999","priceCurrency":"RUB"},"brand":"OzonBrand","sku":"12345","aggregateRating":{"ratingValue":"4.8","reviewCount":"150"}}</script>'''
ozon_text = extract_product_text(ozon_html, "ozon")
assert "Тестовый товар Ozon" in ozon_text
ok("Ozon text extraction from HTML")

# ─── Summary ───
print(f"\n{'='*40}")
print(f"RESULTS: {TESTS_PASSED} passed, {TESTS_FAILED} failed")
print(f"{'='*40}")
sys.exit(0 if TESTS_FAILED == 0 else 1)
