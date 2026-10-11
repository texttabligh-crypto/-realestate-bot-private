from pathlib import Path
import ast


ROOT = Path(__file__).resolve().parent / "app"
NAMES = (
    "bot.py",
    "shahfile_ai.py",
    "shahfile_smart.py",
    "customer_showcase.py",
    "crm_panel.py",
)

files = {}
for name in NAMES:
    text = (ROOT / name).read_text(encoding="utf-8")
    ast.parse(text)
    files[name] = text

bot = files["bot.py"]
ai = files["shahfile_ai.py"]
smart = files["shahfile_smart.py"]
showcase = files["customer_showcase.py"]
crm = files["crm_panel.py"]

checks = {
    "manager_buttons": all(
        value in bot
        for value in (
            "smart_filter_parking",
            "smart_filter_elevator",
            "smart_filter_storage",
            "smart_filter_deed",
            "smart_filter_south",
            "smart_filter_north",
        )
    ),
    "strict_manager_filters": all(
        value in bot
        for value in (
            "p.has_parking",
            "p.has_elevator",
            "p.has_storage",
            "p.deed_text",
            "p.direction",
        )
    ),
    "customer_natural_filters": all(
        value in ai
        for value in (
            "deed_required",
            "direction",
            "has_parking",
            "has_elevator",
            "has_storage",
        )
    ),
    "simple_customer_confirmation": all(
        value in smart
        for value in (
            "sc_search_confirm",
            "sc_search_edit",
            "درسته، فایل‌ها رو نشون بده",
        )
    ),
    "showcase_exact_mapping": all(
        value in showcase
        for value in (
            "deed_required",
            "direction",
            "has_parking",
            "has_elevator",
            "has_storage",
        )
    ),
    "permission_guard": "smart_filter_" in crm,
    "clean_showcase_ui": "cs_visit_" not in showcase,
    "category_single_page_indicator": all(
        value in bot
        for value in (
            "if total <= 0:",
            'f"صفحه {current_page} از {total_pages}"',
            "total=len(all_rows)",
            "await _edit_category_message_with_retry(",
        )
    ),
}

missing = [key for key, passed in checks.items() if not passed]
if missing:
    raise SystemExit("missing checks: " + ", ".join(missing))

print("ADVANCED_SMART_SEARCH_V2_SMOKE_OK")
