from pathlib import Path
import ast


ROOT = Path(__file__).resolve().parent / "app"
NAMES = (
    "bot.py",
    "shahfile_ai.py",
    "shahfile_smart.py",
    "customer_showcase.py",
    "crm_panel.py",
    "property_parser.py",
    "publisher.py",
    "visit_flow.py",
    "visit_leads.py",
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
parser = files["property_parser.py"]
publisher = files["publisher.py"]
visit_flow = files["visit_flow.py"]
visit_leads = files["visit_leads.py"]

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
            "reply_markup=navigation_keyboard",
            "activate_category_post_delivery(",
            "is_active=False",
        )
    ),
    "colloquial_decimal_price": all(
        value in parser
        for value in (
            "colloquial = re.search(",
            'r"(\\d{1,3})\\s*تومان"',
            'return billions + (millions / Decimal("1000"))',
        )
    ),
    "publish_status_column": (
        'if row[-2] != "ACTIVE":' in publisher
        and 'if row[-1] != "ACTIVE":' not in publisher
    ),
    "visit_notification_retry": all(
        value in visit_flow
        for value in (
            "start_visit_notification_worker",
            "run_pending_visit_notifications",
            "visit_notification_retry_task",
        )
    ) and all(
        value in visit_leads
        for value in (
            "get_unnotified_visit_request_ids",
            "notified_at IS NULL",
            "receive_visit_request_notifications",
        )
    ) and "start_visit_notification_worker(application)" in bot,
}

missing = [key for key, passed in checks.items() if not passed]
if missing:
    raise SystemExit("missing checks: " + ", ".join(missing))

from app.property_parser import parse_price_billion

if str(parse_price_billion("قیمت کل: ۵ میلیارد ۶۰۰ تومان")) != "5.6":
    raise SystemExit("colloquial decimal price regression")

print("ADVANCED_SMART_SEARCH_V2_SMOKE_OK")
