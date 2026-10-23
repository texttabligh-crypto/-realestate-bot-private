from html import escape

FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(value):
    return str(value).translate(FA)


def money(value):
    if value in (None, ""):
        return "توافقی"
    value = int(value)
    if value >= 1_000_000_000:
        number = value / 1_000_000_000
        shown = (f"{number:.2f}").rstrip("0").rstrip(".")
        return f"{fa(shown)} میلیارد تومان"
    return f"{fa(f'{value:,}')} تومان"


def price_hashtag(price_toman):
    """ShahFile's price hashtag, e.g. «#آپارتمان_12_13» for 12.5 billion."""
    if not price_toman:
        return None
    lower = int(int(price_toman) / 1_000_000_000)
    return f"#آپارتمان_{lower}_{lower + 1}"


def public_caption(submission, brand, central=False):
    title = "فایل اختصاصی املاک" if central else brand.get("display_name") or "فایل جدید"
    # Opens like a ShahFile post: the price hashtag, then the agency's own unit code.
    head = []
    if submission.get("deal_type") != "RENT" and price_hashtag(submission.get("current_price_toman")):
        head.append(price_hashtag(submission["current_price_toman"]))
    if submission.get("unit_code"):
        head.append(f"کد واحد: {escape(str(submission['unit_code']))}")
    lines = (head + [""] if head else []) + [f"🏠 <b>{escape(title)}</b>", ""]
    mapping = [
        ("district", "📍 منطقه"), ("address_text", "📌 محدوده"),
        ("area_m2", "📐 متراژ"), ("bedrooms", "🛏 خواب"),
        ("floor_text", "🏢 طبقه"), ("build_year", "📅 سال ساخت"),
        ("direction", "🧭 جهت"), ("parking_text", "🚗 پارکینگ"),
        ("elevator_text", "🛗 آسانسور"), ("storage_text", "📦 انباری"),
        ("deed_text", "📄 سند"), ("installations_text", "🔧 امکانات"),
    ]
    for key, label in mapping:
        value = submission.get(key)
        if value not in (None, "", "__SKIP__"):
            suffix = " متر" if key == "area_m2" else ""
            if key == "area_m2":
                value = (f"{float(value):.2f}").rstrip("0").rstrip(".")  # «184.00» -> «184»
            lines.append(f"{label}: {escape(fa(value))}{suffix}")
    if submission.get("deal_type") == "RENT":
        lines += [f"💰 رهن: {money(submission.get('deposit_toman'))}", f"💳 اجاره: {money(submission.get('rent_toman'))}"]
    else:
        lines.append(f"💰 قیمت کل: {money(submission.get('current_price_toman'))}")
    if submission.get("special_notes"):
        lines += ["", escape(submission["special_notes"])]
    lines.append("")
    if not central:
        if brand.get("public_phone"): lines.append(f"☎️ {escape(fa(brand['public_phone']))}")
        if brand.get("public_mobile"): lines.append(f"📱 {escape(fa(brand['public_mobile']))}")
        if brand.get("telegram_username"): lines.append(f"💬 @{escape(brand['telegram_username'].lstrip('@'))}")
        if brand.get("footer_text"): lines += ["", escape(brand["footer_text"])]
        if brand.get("cta_text"): lines += ["", f"🔔 {escape(brand['cta_text'])}"]
    return "\n".join(lines)[:4000]
