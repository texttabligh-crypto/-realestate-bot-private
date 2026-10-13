from pathlib import Path
import ast


PROJECT_ROOT = Path(__file__).resolve().parent
ROOT = PROJECT_ROOT / "app"
PARTNER_APP = (PROJECT_ROOT / "partner_hub" / "app.py").read_text(
    encoding="utf-8"
)
PARTNER_DB = (PROJECT_ROOT / "partner_hub" / "db.py").read_text(
    encoding="utf-8"
)
PARTNER_MIGRATION = (
    PROJECT_ROOT / "partner_hub" / "migrations" / "002_team_availability.sql"
).read_text(encoding="utf-8")
ast.parse(PARTNER_APP)
ast.parse(PARTNER_DB)
NAMES = (
    "bot.py",
    "shahfile_ai.py",
    "shahfile_smart.py",
    "customer_showcase.py",
    "crm_panel.py",
    "crm_campaign_hub.py",
    "property_parser.py",
    "publisher.py",
    "visit_flow.py",
    "visit_leads.py",
    "visit_ops_group.py",
    "scheduler.py",
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
campaign_hub = files["crm_campaign_hub.py"]
parser = files["property_parser.py"]
publisher = files["publisher.py"]
visit_flow = files["visit_flow.py"]
visit_leads = files["visit_leads.py"]
visit_ops = files["visit_ops_group.py"]
scheduler = files["scheduler.py"]

checks = {
    "partner_hub_blue_menu": all(
        value in PARTNER_APP
        for value in (
            "BotCommand",
            "MenuButtonCommands",
            "configure_blue_menu",
            'BotCommand("start", "شروع ربات")',
            'BotCommand("menu", "منوی اصلی")',
            "await configure_blue_menu(app)",
        )
    ),
    "partner_team_and_three_day_availability": all(
        value in PARTNER_APP
        for value in (
            "TEAM_PERMISSIONS",
            "team_invrole_SECRETARY",
            "team_invuses_",
            "TEAM_MEMBER_DEACTIVATED",
            "Partner three-day availability worker started.",
            "availability_check_due_at",
            "availability_missed_cycles",
            "interval '3 days'",
            "interval '1 hour'",
            "tenant availability delivery unavailable; using hub bot",
        )
    ) and all(
        value in PARTNER_DB
        for value in (
            "ensure_partner_v2_schema",
            "can_manage_team",
            "can_view_stats",
            "availability_check_due_at",
            "availability_missed_cycles",
            "interval '3 days'",
        )
    ) and all(
        value in PARTNER_MIGRATION
        for value in (
            "SECRETARY",
            "can_manage_team",
            "can_view_stats",
            "is_revoked",
            "availability_check_due_at",
        )
    ),
    "scheduler_supervisor_watchdog": all(
        value in scheduler
        for value in (
            "scheduler_runtime_state",
            "run_guarded_scheduler_tick",
            "scheduler_watchdog_loop",
            "scheduler_watchdog_task",
            "asyncio.wait_for(",
            "SKIPPED_STALE",
        )
    ),
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
    "automatic_latest_search_subscription": (
        "activate_search_subscription(" in ai
        and "subscription_id = result[3]" in ai
        and "The newest confirmed search is active automatically" in smart
        and "می‌خواین در روزهای آینده هم هر فایل جدیدی" not in smart
        and "عضویت شما در این دسته فعال شد" in showcase
    ),
    "single_opt_out_footer": (
        'lines.extend(["", OPT_OUT_TEXT])' in ai
        and "sent_any = False" in ai
        and "text=OPT_OUT_TEXT" in ai
        and 'lines.extend(["", OPT_OUT_TEXT])\n    return "\\n".join(lines)' not in ai
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
    "live_lead_campaigns": all(
        value in showcase
        for value in (
            "customer_campaign_deliveries",
            "complete_showcase_registration",
            "start_campaign_watch_worker",
            "campaign_summary_text",
            "SHOWCASE_PHONE",
            "PENDING",
            "SENT",
        )
    ) and all(
        value in bot
        for value in (
            "start_campaign_watch_worker(application)",
            "customer_campaigns",
            "ساخت لینک جذب مشتری",
        )
    ),
    "deleted_destination_catalog_cleanup": all(
        value in bot
        for value in (
            "_mark_destination_post_deleted",
            "set(watched) - set(observed)",
            "destination-public-post-deleted",
            "deleted_count",
        )
    ),
    "source_location_fidelity_and_backfill": all(
        value in parser
        for value in (
            "_extract_headline_location",
            "strict extension of a shorter",
            "headline_location",
        )
    ) and all(
        value in bot
        for value in (
            "FORCED_SOURCE_ADDRESS_BY_UNIT_CODE",
            '"31123": "ولیعصر لیدای جدید"',
            "_forced_location_repair_rows",
            "p.unit_code::text = ANY(%s)",
            "Forced source location post refresh failed",
            "_extended_location_repair_rows",
            "repair_extended_source_locations",
            "best_candidates",
            "forced_posts=%s source_rows=%s candidates=%s posts=%s",
            "delayed_extended_source_location_repair",
            "await asyncio.sleep(15)",
            "current_address_text not in new_address_text",
            "start_extended_source_location_repair(application)",
        )
    ),
    "explicit_source_facility_repair": all(
        value in bot
        for value in (
            'FORCED_NO_ELEVATOR_UNIT_CODES = {"25610"}',
            "_explicit_feature_repair_rows",
            "_set_explicit_missing_features",
            '"parking" in missing_features',
            '"elevator" in missing_features',
            '"storage" in missing_features',
            "start_explicit_source_feature_repair(application)",
        )
    ),
    "bazdid_file_visit_operations": all(
        value in visit_ops
        for value in (
            "visit_ops_destination",
            "handle_connect_command",
            "message_thread_id",
            "گروه عملیات بازدید متصل شد",
        )
    ) and all(
        value in bot
        for value in (
            "ensure_visit_ops_schema()",
            'CommandHandler("connect_bazdid"',
        )
    ) and all(
        value in visit_flow
        for value in (
            "get_visit_ops_target",
            "telegram_thread_kwargs",
            "delivery_targets",
        )
    ) and all(
        value in smart
        for value in (
            "get_visit_ops_target",
            "recipients",
            "thread_kwargs",
        )
    ) and all(
        value in crm
        for value in (
            "get_visit_ops_target",
            "telegram_thread_kwargs",
            "recipients",
        )
    ),
    "crm_campaign_broadcast_and_support": all(
        value in campaign_hub
        for value in (
            "crm_campaign_broadcasts",
            "crm_campaign_broadcast_deliveries",
            "crm_support_tickets",
            "crm_campaigns",
            "crm_support_inbox",
            "escalate_customer_question",
            "CRM campaign broadcast worker started.",
            "phone_collected_at IS NOT NULL",
        )
    ) and all(
        value in crm
        for value in (
            "CRM CAMPAIGN HUB V4",
            "🎯 دسته‌های لینک",
            "💬 سؤال‌های مشتریان",
        )
    ) and all(
        value in smart
        for value in (
            "escalate_customer_question",
            "سؤالتون برای مشاور ارسال شد",
        )
    ) and all(
        value in bot
        for value in (
            "ensure_campaign_crm_schema()",
            "start_campaign_broadcast_worker(application)",
        )
    ),
    "first_visit_funding_profile": all(
        value in smart
        for value in (
            "customer_purchase_profiles",
            "ask_finance_once",
            "set_customer_funding_status",
            '"CASH"',
            '"SELL_PROPERTY"',
            '"SHOWCASE_PHONE"',
        )
    ) and "ask_finance_once" in visit_flow,
    "visit_source_direct_link": all(
        value in visit_leads
        for value in (
            "_find_source_by_identity",
            "_direct_source_post_url",
            "source_is_active",
            "A channel root such as",
        )
    ) and all(
        value in visit_flow
        for value in (
            "_build_operator_source_keyboard",
            "handle_visit_source_callback",
            "این واحد از کانال منبع حذف شده.",
            'callback_data=(',
        )
    ) and all(
        value in bot
        for value in (
            "handle_visit_source_callback",
            'pattern=r"^visitsrc_"',
            'pattern=r"^(?!catnav_|regcat_|visitsrc_)"',
        )
    ),
}

missing = [key for key, passed in checks.items() if not passed]
if missing:
    raise SystemExit("missing checks: " + ", ".join(missing))

from app.property_parser import (
    parse_dynamic_attributes,
    parse_price_billion,
    parse_property,
)
from app.phone_utils import normalize_iran_phone

if str(parse_price_billion("قیمت کل: ۵ میلیارد ۶۰۰ تومان")) != "5.6":
    raise SystemExit("colloquial decimal price regression")

extended_location = parse_property(
    "پیش فروش آپارتمان | ولیعصر لیدای جدید\n"
    "۱۲۵ متر | ۲ خواب"
)
if extended_location["district"] != "ولیعصر":
    raise SystemExit("extended location district regression")
if extended_location["address_text"] != "ولیعصر لیدای جدید":
    raise SystemExit("extended location address regression")

extended_location_with_short_pin = parse_property(
    "📍 ولیعصر\n"
    "پیش فروش آپارتمان | ولیعصر لیدای جدید\n"
    "۱۲۵ متر | ۲ خواب"
)
if (
    extended_location_with_short_pin["address_text"]
    != "ولیعصر لیدای جدید"
):
    raise SystemExit("short pin masked precise headline regression")

negative_elevator_samples = (
    "بدون آسانسور",
    "آسانسور ندارد",
    "آسانسور: نداره",
    "آسانسور نمی‌باشد",
    "آسانسور ❌",
)
for sample in negative_elevator_samples:
    if parse_property(sample)["has_elevator"] is not False:
        raise SystemExit(f"negative elevator regression: {sample}")

if parse_property("دارای آسانسور")["has_elevator"] is not True:
    raise SystemExit("positive elevator regression")

source_detail_sample = """
✨ ویژگی‌های ملک
▪️ سازنده مطمئن
▪️ پاسیو حذف/خواب‌ها دارای پنجره✅
"""
source_detail_values = [
    item["attribute_value"]
    for item in parse_dynamic_attributes(source_detail_sample)
    if item["attribute_key"].startswith("source_detail_")
]
if not any("سازنده مطمئن" in value for value in source_detail_values):
    raise SystemExit("builder source detail regression")
if not any(
    "پاسیو حذف" in value and "پنجره" in value
    for value in source_detail_values
):
    raise SystemExit("patio/window source detail regression")

def deed_value(sample):
    for item in parse_dynamic_attributes(sample):
        if item["attribute_key"] == "deed_type":
            return item["attribute_value"]
    return None

if deed_value("نصبیات کامل؛ سند قراردادی") != "قراردادی":
    raise SystemExit("contract deed regression")

if deed_value("سند قول نامه ای") != "قولنامه‌ای":
    raise SystemExit("promissory deed regression")

phone_cases = {
    "+98 935 938 4979": "09359384979",
    "989359384979": "09359384979",
    "9359384979": "09359384979",
    "۰۹۳۵۹۳۸۴۹۷۹": "09359384979",
}
for raw_phone, expected_phone in phone_cases.items():
    if normalize_iran_phone(raw_phone) != expected_phone:
        raise SystemExit(f"phone normalization regression: {raw_phone}")

print("ADVANCED_SMART_SEARCH_V2_SMOKE_OK")
