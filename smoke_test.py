from pathlib import Path
import ast


ROOT = Path(__file__).resolve().parent / "app"
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

from app.property_parser import parse_price_billion

if str(parse_price_billion("قیمت کل: ۵ میلیارد ۶۰۰ تومان")) != "5.6":
    raise SystemExit("colloquial decimal price regression")

print("ADVANCED_SMART_SEARCH_V2_SMOKE_OK")
