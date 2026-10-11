from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone

import psycopg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from shahfile_ai import activate_search_subscription, decline_search_subscription
from shahfile_smart import upsert_customer_from_telegram


PAGE_SIZE = 6
BOT_USERNAME_CACHE = None


def db_connect():
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


def ensure_showcase_schema():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_showcases (
                    id BIGSERIAL PRIMARY KEY,
                    token VARCHAR(40) NOT NULL UNIQUE,
                    created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    query_text TEXT NOT NULL,
                    filters JSONB NOT NULL DEFAULT '{}'::jsonb,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    disabled_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_showcase_items (
                    showcase_id BIGINT NOT NULL REFERENCES customer_showcases(id) ON DELETE CASCADE,
                    property_id BIGINT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (showcase_id, property_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_showcase_visitors (
                    showcase_id BIGINT NOT NULL REFERENCES customer_showcases(id) ON DELETE CASCADE,
                    telegram_user_id BIGINT NOT NULL,
                    customer_id BIGINT REFERENCES customer_profiles(id) ON DELETE SET NULL,
                    subscription_id BIGINT REFERENCES smart_search_subscriptions(id) ON DELETE SET NULL,
                    first_opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    open_count INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (showcase_id, telegram_user_id)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_showcase_items_position ON customer_showcase_items(showcase_id, position)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_showcase_visitors_user ON customer_showcase_visitors(telegram_user_id, last_opened_at DESC)")
        conn.commit()


def manager_filters_to_public(filters):
    filters = dict(filters or {})
    out = {}
    for source, target in {
        "price_min": "price_min_billion",
        "price_max": "price_max_billion",
        "area_min": "area_min",
        "area_max": "area_max",
        "bedrooms": "bedrooms",
    }.items():
        if filters.get(source) is not None:
            out[target] = filters[source]
    if filters.get("free_text"):
        out["free_text"] = filters["free_text"]
    if filters.get("floor_text"):
        out["floor_text"] = filters["floor_text"]
    if filters.get("unit_code"):
        out["unit_code"] = filters["unit_code"]
    for key in ("has_parking", "has_elevator", "has_storage", "deed_required"):
        if filters.get(key) is True:
            out[key] = True
    if filters.get("direction"):
        out["direction"] = filters["direction"]
    return out


def create_showcase(created_by_user_id, query_text, filters, property_ids):
    ids, seen = [], set()
    for value in property_ids:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            continue
        if pid not in seen:
            ids.append(pid)
            seen.add(pid)
    if not ids:
        return None
    token = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]
    filters = dict(filters or {})
    filters["created_after"] = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO customer_showcases(token,created_by_user_id,query_text,filters)
                VALUES (%s,%s,%s,%s::jsonb) RETURNING id,expires_at
            """, (token, created_by_user_id, query_text, json.dumps(filters, ensure_ascii=False)))
            showcase_id, expires_at = cur.fetchone()
            cur.executemany("""
                INSERT INTO customer_showcase_items(showcase_id,property_id,position)
                VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
            """, [(showcase_id, pid, index) for index, pid in enumerate(ids, start=1)])
        conn.commit()
    return {"id": int(showcase_id), "token": token, "expires_at": expires_at, "count": len(ids)}


async def build_showcase_link(bot, token):
    global BOT_USERNAME_CACHE
    if not BOT_USERNAME_CACHE:
        me = await bot.get_me()
        BOT_USERNAME_CACHE = me.username if me else None
    if not BOT_USERNAME_CACHE:
        raise RuntimeError("Bot username is unavailable")
    return f"https://t.me/{BOT_USERNAME_CACHE}?start=showcase_{token}"


def _load_showcase(token):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id,token,query_text,filters,expires_at,created_by_user_id
                FROM customer_showcases
                WHERE token=%s AND is_active=TRUE AND expires_at>NOW()
            """, (token,))
            row = cur.fetchone()
    if not row:
        return None
    return {"id": int(row[0]), "token": row[1], "query_text": row[2], "filters": dict(row[3] or {}), "expires_at": row[4], "created_by_user_id": row[5]}


def _live_item_ids(showcase_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.property_id FROM customer_showcase_items i
                JOIN properties p ON p.id=i.property_id
                WHERE i.showcase_id=%s AND p.status='ACTIVE'
                  AND p.published_message_id IS NOT NULL
                  AND p.deleted_from_channel_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM property_trash pt WHERE pt.property_id=p.id)
                ORDER BY i.position
            """, (showcase_id,))
            return [int(row[0]) for row in cur.fetchall()]


def _ensure_visitor(showcase, user):
    profile = upsert_customer_from_telegram(user)
    customer_id = profile[0] if profile else None
    snapshot_ids = _live_item_ids(showcase["id"])
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT subscription_id FROM customer_showcase_visitors WHERE showcase_id=%s AND telegram_user_id=%s", (showcase["id"], user.id))
            found = cur.fetchone()
            subscription_id = found[0] if found else None
            if not subscription_id:
                cur.execute("""
                    INSERT INTO smart_search_subscriptions(
                        telegram_user_id,customer_id,query_text,filters,is_active,
                        created_at,updated_at,cancelled_at
                    ) VALUES (%s,%s,%s,%s::jsonb,FALSE,NOW(),NOW(),NULL)
                    RETURNING id
                """, (user.id, customer_id, showcase["query_text"], json.dumps(showcase["filters"], ensure_ascii=False)))
                subscription_id = int(cur.fetchone()[0])
                if snapshot_ids:
                    cur.executemany("""
                        INSERT INTO smart_search_seen(subscription_id,property_id,seen_at)
                        VALUES (%s,%s,NOW()) ON CONFLICT DO NOTHING
                    """, [(subscription_id, pid) for pid in snapshot_ids])
                cur.execute("""
                    INSERT INTO customer_showcase_visitors(showcase_id,telegram_user_id,customer_id,subscription_id)
                    VALUES (%s,%s,%s,%s)
                """, (showcase["id"], user.id, customer_id, subscription_id))
            else:
                cur.execute("""
                    UPDATE customer_showcase_visitors
                    SET customer_id=COALESCE(customer_id,%s),last_opened_at=NOW(),open_count=open_count+1
                    WHERE showcase_id=%s AND telegram_user_id=%s
                """, (customer_id, showcase["id"], user.id))
        conn.commit()
    return int(subscription_id)


def _page(showcase_id, offset=0):
    base = """
        FROM customer_showcase_items i JOIN properties p ON p.id=i.property_id
        WHERE i.showcase_id=%s AND p.status='ACTIVE'
          AND p.published_message_id IS NOT NULL AND p.deleted_from_channel_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM property_trash pt WHERE pt.property_id=p.id)
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) " + base, (showcase_id,))
            total = int(cur.fetchone()[0] or 0)
            cur.execute("""
                SELECT p.id,p.unit_code,p.district,p.address_text,p.area_m2,
                       p.bedrooms,p.build_year,p.current_price_billion,
                       p.channel_post_url,p.published_message_id
            """ + base + " ORDER BY i.position LIMIT %s OFFSET %s", (showcase_id, PAGE_SIZE, offset))
            rows = cur.fetchall()
    return rows, total


def _fmt(value):
    return "-" if value is None else f"{float(value):g}"


def render_showcase(showcase, subscription_id, offset=0):
    rows, total = _page(showcase["id"], offset)
    if total:
        offset = min(max(0, offset), ((total - 1) // PAGE_SIZE) * PAGE_SIZE)
        if not rows:
            rows, total = _page(showcase["id"], offset)
    lines = ["🏠 ویترین اختصاصی شما", "", f"🔎 درخواست: {showcase['query_text']}", f"📂 فایل‌های فعال: {total}", ""]
    if not rows:
        lines.extend(["در حال حاضر هیچ‌کدام از فایل‌های این ویترین فعال نیستند.", "اگر اعلان را فعال کنید، گزینه مناسب بعدی جداگانه برایتان ارسال می‌شود."])
    for index, row in enumerate(rows, start=offset + 1):
        _pid, unit, district, address, area, beds, year, price, url, message_id = row
        lines.append(f"{index}) 📍 {address or district or 'بدون منطقه'}")
        details = []
        if area is not None: details.append(f"📐 {_fmt(area)} متر")
        if beds is not None: details.append(f"🛏 {beds} خواب")
        if year: details.append(f"📅 سال ساخت {year}")
        if details: lines.append(" | ".join(details))
        if price is not None: lines.append(f"💰 قیمت: {_fmt(price)} میلیارد تومان")
        lines.append(f"🔢 کد واحد: {unit or '-'}")
        final_url = url or (f"https://t.me/nimatmelk/{message_id}" if message_id else None)
        if final_url: lines.append(f"🔗 مشاهده فایل: {final_url}")
        lines.extend(["", "──────────────", ""])
    lines.extend([
        "📅 برای گرفتن وقت بازدید، فقط کافیه کلمه «بازدید» رو به همراه کد فایل ارسال کنید.",
        "مثال: بازدید ۲۷۲۹",
    ])
    keyboard, nav = [], []
    if offset > 0: nav.append(InlineKeyboardButton("◀️ فایل‌های قبلی", callback_data=f"cs_page_{showcase['token']}_{max(0, offset-PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total: nav.append(InlineKeyboardButton("فایل‌های دیگر ▶️", callback_data=f"cs_page_{showcase['token']}_{offset+PAGE_SIZE}"))
    if nav: keyboard.append(nav)
    markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    return "\n".join(lines).strip(), markup


def _watch_offer_keyboard(subscription_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "✅ بله، فایل‌های جدید رو بفرست",
            callback_data=f"cs_watch_yes_{subscription_id}",
        )],
        [InlineKeyboardButton(
            "❌ نه، لازم نیست",
            callback_data=f"cs_watch_no_{subscription_id}",
        )],
    ])


async def handle_showcase_start(update, context):
    if not context.args or not context.args[0].startswith("showcase_"):
        return False
    showcase = _load_showcase(context.args[0][len("showcase_"):])
    if not showcase:
        await update.message.reply_text("این لینک منقضی یا غیرفعال شده است. برای دریافت فایل‌های جدید با مشاور خود تماس بگیرید.")
        return True
    subscription_id = _ensure_visitor(showcase, update.effective_user)
    text, keyboard = render_showcase(showcase, subscription_id, 0)
    await update.message.reply_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    await update.message.reply_text(
        "🔔 می‌خواین در روزهای آینده هم هر فایل جدیدی که دقیقاً با همین درخواست جور بود، جداگانه براتون ارسال کنم؟\n\n"
        "هر وقت نخواستید، فقط کلمه «لغو» رو بفرستید.",
        reply_markup=_watch_offer_keyboard(subscription_id),
    )
    return True


async def handle_showcase_callback(update, context, session=None):
    query, action = update.callback_query, (update.callback_query.data or "")
    if not action.startswith("cs_"):
        return False
    user_id = update.effective_user.id
    if action.startswith("cs_page_"):
        try:
            token, offset_text = action[len("cs_page_"):].rsplit("_", 1)
            offset = max(0, int(offset_text))
        except Exception:
            await query.message.reply_text("صفحه نامعتبر است."); return True
        showcase = _load_showcase(token)
        if not showcase:
            await query.edit_message_text("این لینک منقضی یا غیرفعال شده است."); return True
        subscription_id = _ensure_visitor(showcase, update.effective_user)
        text, keyboard = render_showcase(showcase, subscription_id, offset)
        await query.edit_message_text(text, reply_markup=keyboard, disable_web_page_preview=True)
        return True
    if action.startswith("cs_watch_yes_") or action.startswith("cs_watch_no_"):
        try:
            subscription_id = int(action.rsplit("_", 1)[1])
        except Exception:
            await query.message.reply_text("درخواست نامعتبر است."); return True
        if action.startswith("cs_watch_yes_"):
            ok = activate_search_subscription(subscription_id, user_id)
            await query.message.reply_text("اعلان فایل‌های جدید فعال شد ✅" if ok else "این درخواست در دسترس نیست.")
        else:
            decline_search_subscription(subscription_id, user_id)
            await query.message.reply_text("اعلان فایل‌های مشابه متوقف شد.")
        return True
    return False
