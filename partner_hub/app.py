from __future__ import annotations
import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot, BotCommand, InlineKeyboardButton as B, InlineKeyboardMarkup as M, MenuButtonCommands, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import central_channel as central
import channel_tools as ct
import db
from formatter import fa, money
from security import decrypt_token, encrypt_token, token_fingerprint

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("partner-hub")
TEHRAN = ZoneInfo("Asia/Tehran")
OWNER_ID = int(os.getenv("PARTNER_OWNER_TELEGRAM_ID", "146752428"))
HUB_TOKEN = os.environ["PARTNER_HUB_TOKEN"]
SKIP = "__SKIP__"

FIELDS = [
    ("property_type","نوع ملک",False),("deal_type","نوع معامله",False),
    ("district","منطقه",False),("address_text","محله / خیابان / آدرس",False),
    ("area_m2","متراژ",False),("bedrooms","تعداد خواب",True),("floor_text","طبقه",True),
    ("total_floors","تعداد کل طبقات",True),("units_per_floor","تعداد واحد در طبقه",True),
    ("build_year","سال ساخت",True),("direction","جهت ملک",True),
    ("parking_text","پارکینگ",True),("elevator_text","آسانسور",True),("storage_text","انباری",True),
    ("deed_text","سند",True),("installations_text","نصبیات / امکانات",True),
    ("current_price_toman","قیمت کل",True),("deposit_toman","مبلغ رهن",True),("rent_toman","اجاره ماهانه",True),
    ("special_notes","توضیحات ویژه",True),("owner_name","نام مالک (خصوصی)",False),
    ("owner_phone","شماره مالک (خصوصی)",False),("owner_address_private","آدرس دقیق مالک/ملک (خصوصی)",True),
]
TYPE_KEYS = {"آپارتمان":"APARTMENT","ویلایی":"VILLA","زمین":"LAND","تجاری":"COMMERCIAL","اداری":"OFFICE","کلنگی":"OLD_HOUSE"}
DEAL_KEYS = {"فروش":"SALE","رهن و اجاره":"RENT","مشارکت":"PARTNERSHIP","معاوضه":"EXCHANGE"}
ROLE_LABELS = {
    "AGENCY_OWNER": "مالک بنگاه",
    "AGENCY_ADMIN": "مدیر",
    "ADVISOR": "مشاور",
    "SECRETARY": "منشی",
}
TEAM_PERMISSIONS = {
    "can_add_file": "ثبت فایل",
    "can_publish_own": "انتشار در کانال بنگاه",
    "can_request_central": "ارسال برای کانال مرکزی",
    "can_manage_brand": "مدیریت برند",
    "can_manage_team": "مدیریت تیم",
    "can_view_stats": "مشاهده آمار",
}
AVAILABILITY_INTERVAL = timedelta(days=3)
AVAILABILITY_REMINDER_DELAY = timedelta(days=1)
AVAILABILITY_CYCLE_TIMEOUT = timedelta(days=3)


def digits(value):
    return str(value or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩","01234567890123456789"))


def int_value(value, optional=False):
    found = re.sub(r"[^0-9]", "", digits(value))
    if not found and optional: return None
    if not found: raise ValueError("number")
    return int(found)


def price(value):
    raw = digits(value).replace("٬",",").replace("٫",".").replace("،",",").lower()
    number = re.sub(r"[^0-9.,]", "", raw).replace(",", "")
    if not number: raise ValueError("price")
    n = float(number)
    if "میلیارد" in raw or (n < 1000 and "میلیون" not in raw and "تومان" not in raw): n *= 1_000_000_000
    elif "میلیون" in raw or (1000 <= n < 100000 and "تومان" not in raw): n *= 1_000_000
    if n <= 0: raise ValueError("price")
    return int(round(n))


def clean_phone(value):
    value = re.sub(r"[^0-9+]", "", digits(value))
    if len(re.sub(r"\D", "", value)) < 8: raise ValueError("phone")
    return value


async def configure_blue_menu(application: Application):
    """Expose Telegram's blue Menu button for hub and tenant bots."""
    await application.bot.set_my_commands(
        [
            BotCommand("start", "شروع ربات"),
            BotCommand("menu", "منوی اصلی"),
        ]
    )
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )
    me = await application.bot.get_me()
    log.info(
        "blue menu configured bot=@%s id=%s",
        me.username,
        me.id,
    )


def home_keyboard(member=None, owner=False):
    rows=[]
    if owner:
        rows += [
            [B("🏢 درخواست‌های عضویت",callback_data="o_pending"),B("🧾 صف بررسی فایل",callback_data="o_submissions")],
            [B("📊 آمار شبکه",callback_data="o_stats")],
        ]
    if member and member["agency_status"] == "ACTIVE":
        first=[]
        if member.get("can_add_file"):
            first.append(B("➕ ثبت فایل جدید",callback_data="p_add"))
        first.append(B("🏠 فایل‌های من",callback_data="p_files"))
        rows.append(first)
        if member["role"]=="AGENCY_OWNER" or member.get("can_manage_brand"):
            rows.append([
                B("🎨 برند و وایت‌لیبل",callback_data="p_brand"),
                B("🔌 اتصال ربات و کانال",callback_data="p_connect"),
            ])
        if can_publish(member):
            rows.append([B("⏰ زمان‌بندی ارسال به کانال",callback_data="p_sched")])
        team_stats=[]
        if member["role"]=="AGENCY_OWNER" or member.get("can_manage_team"):
            team_stats.append(B("👥 تیم و دعوت",callback_data="p_team"))
        if member["role"]=="AGENCY_OWNER" or member.get("can_view_stats"):
            team_stats.append(B("📊 آمار بنگاه",callback_data="p_stats"))
        if team_stats:
            rows.append(team_stats)
    elif not member and not owner:
        rows += [[B("🏢 ثبت‌نام بنگاه",callback_data="reg_start")],[B("🔑 ورود با کد دعوت",callback_data="join_start")]]
    return M(rows)


async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; mem=db.member(uid)
    agency = db.bot_agency(context.bot.id)
    if agency and (not mem or mem["agency_id"] != agency["id"]):
        text=f"به ربات اختصاصی «{agency['brand_name']}» خوش آمدید.\nبرای ورود باید عضو تأییدشده همین مجموعه باشید."
    else:
        text="🏙 سامانه یکپارچه همکاران املاک\n\nفایل را یک‌بار ثبت کنید؛ با هویت بنگاه خودتان در کانال شما و پس از بررسی در شبکه مرکزی منتشر می‌شود. اطلاعات مالک عمومی نخواهد شد."
    await update.effective_message.reply_text(text,reply_markup=home_keyboard(mem,uid==OWNER_ID))


async def menu(update,context): await start(update,context)


async def on_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer(); a=q.data; uid=q.from_user.id; mem=db.member(uid)
    scoped=db.bot_agency(context.bot.id)
    if scoped and (not mem or mem['agency_id']!=scoped['id']):
        await q.edit_message_text("این ربات اختصاصی فقط برای اعضای همین مجموعه فعال است.")
        return
    if a=="home": await q.edit_message_text("منوی اصلی",reply_markup=home_keyboard(mem,uid==OWNER_ID)); return
    if a=="reg_start":
        context.user_data.clear(); context.user_data.update(flow="register",step=0,data={})
        await q.edit_message_text("نام رسمی بنگاه را ارسال کنید:"); return
    if a=="join_start":
        context.user_data.clear(); context.user_data["flow"]="join"
        await q.edit_message_text("کد دعوت را ارسال کنید:"); return
    if uid==OWNER_ID and a.startswith("o_"):
        await owner_callback(q,context,a); return
    if not mem or mem["agency_status"]!="ACTIVE":
        await q.edit_message_text("عضویت شما هنوز فعال نیست.",reply_markup=home_keyboard(mem,uid==OWNER_ID)); return
    if a.startswith("avail_"):
        await availability_callback(q,context,a,mem); return
    if a=="p_add":
        if not mem.get("can_add_file"):
            await q.answer("دسترسی ثبت فایل ندارید.",show_alert=True); return
        await begin_property(q,context); return
    if a.startswith("pf_") or a.startswith("ptype_") or a.startswith("deal_"):
        await property_callback(q,context,a,mem); return
    if a=="p_files" or a.startswith("p_files_"):
        await list_files(q,mem,int(a.rsplit("_",1)[1]) if a.startswith("p_files_") else 0); return
    if a.startswith("pfile_"): await show_file(q,mem,int(a.rsplit("_",1)[1])); return
    if a.startswith("pfsend_") or a.startswith("pfslot_"):
        await send_file_to_channel(q,mem,int(a.rsplit("_",1)[1]),queue=a.startswith("pfslot_")); return
    if a.startswith("pdel_") or a.startswith("pdelok_"): await delete_file(q,mem,a); return
    if a in ("pback","pbackok"): await backfill_files(q,mem,a=="pbackok"); return
    if a=="p_sched" or a.startswith("psched_"): await schedule_callback(q,context,a,mem); return
    if a=="p_brand":
        if not (mem["role"]=="AGENCY_OWNER" or mem.get("can_manage_brand")):
            await q.answer("دسترسی مدیریت برند ندارید.",show_alert=True); return
        await brand_menu(q,mem); return
    if a.startswith("brand_"): await brand_callback(q,context,a,mem); return
    if a=="p_connect":
        if not (mem["role"]=="AGENCY_OWNER" or mem.get("can_manage_brand")):
            await q.answer("دسترسی مدیریت اتصال‌ها ندارید.",show_alert=True); return
        await connection_menu(q,mem); return
    if a.startswith("conn_"): await connection_callback(q,context,a,mem); return
    if a=="p_team" or a.startswith("team_"):
        await team_callback(q,context,a,mem); return
    if a=="p_stats":
        if not (mem["role"] == "AGENCY_OWNER" or mem.get("can_view_stats")):
            await q.answer("دسترسی مشاهده آمار ندارید.",show_alert=True); return
        await agency_stats(q,mem); return


REG_FIELDS=[("legal_name","نام رسمی بنگاه"),("brand_name","نام تجاری"),("city","شهر"),("address_text","آدرس دفتر"),("license_number","شماره جواز کسب؛ اگر ندارید خط تیره بفرستید"),("manager_name","نام مدیر"),("manager_phone","شماره تماس مدیر")]


async def handle_text(update:Update, context:ContextTypes.DEFAULT_TYPE):
    scoped=db.bot_agency(context.bot.id); mem=db.member(update.effective_user.id)
    if scoped and (not mem or mem['agency_id']!=scoped['id']):
        await update.message.reply_text("این ربات اختصاصی فقط برای اعضای همین مجموعه فعال است.")
        return
    flow=context.user_data.get("flow")
    if flow=="register": await registration_text(update,context); return
    if flow=="join": await join_text(update,context); return
    if flow=="property": await property_text(update,context); return
    if flow=="brand": await brand_text(update,context); return
    if flow=="connect_bot": await connect_bot_text(update,context); return
    if flow=="connect_channel": await connect_channel_text(update,context); return
    if flow=="schedule": await schedule_text(update,context); return
    if flow=="slot_times": await slot_times_text(update,context); return
    if flow=="availability_price": await availability_price_text(update,context); return
    await update.message.reply_text("از منوی اصلی انتخاب کنید.",reply_markup=home_keyboard(db.member(update.effective_user.id),update.effective_user.id==OWNER_ID))


async def registration_text(update,context):
    st=context.user_data; idx=st["step"]; key,_=REG_FIELDS[idx]; value=update.message.text.strip()
    if key=="manager_phone":
        try:value=clean_phone(value)
        except: await update.message.reply_text("شماره معتبر ارسال کنید."); return
    st["data"][key]=value; st["step"]+=1
    if st["step"]<len(REG_FIELDS):
        await update.message.reply_text(REG_FIELDS[st["step"]][1]+":"); return
    d=st["data"]; u=update.effective_user
    with db.connect() as c,c.cursor() as cur:
        cur.execute("""INSERT INTO partner_agencies(legal_name,brand_name,slug,city,address_text,license_number,manager_name,manager_phone)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (d['legal_name'],d['brand_name'],db.slugify(d['brand_name']),d['city'],d['address_text'],d['license_number'],d['manager_name'],d['manager_phone']))
        aid=cur.fetchone()['id']
        cur.execute("""INSERT INTO partner_members(agency_id,telegram_user_id,username,display_name,phone,role,can_publish_own,can_manage_brand)
                       VALUES(%s,%s,%s,%s,%s,'AGENCY_OWNER',TRUE,TRUE)""",(aid,u.id,u.username,u.full_name,d['manager_phone']))
        cur.execute("INSERT INTO partner_brand_settings(agency_id,display_name,public_mobile) VALUES(%s,%s,%s)",(aid,d['brand_name'],d['manager_phone']))
        c.commit()
    db.audit("AGENCY_REGISTERED",u.id,aid,"AGENCY",aid)
    context.user_data.clear()
    await update.message.reply_text("✅ درخواست ثبت شد. پس از بررسی مدیر شبکه، پنل بنگاه فعال می‌شود.")
    try: await context.bot.send_message(OWNER_ID,f"🏢 درخواست بنگاه جدید\n{d['brand_name']} — {d['city']}\nمدیر: {d['manager_name']}\nتماس: {d['manager_phone']}",reply_markup=M([[B("✅ تأیید",callback_data=f"o_approve_{aid}"),B("❌ رد",callback_data=f"o_reject_{aid}")]]))
    except TelegramError: log.exception("owner notification failed")


async def join_text(update,context):
    code=update.message.text.strip().upper()
    u=update.effective_user
    current=db.member(u.id)
    if current:
        context.user_data.clear()
        if current["agency_id"]:
            await update.message.reply_text(
                "شما از قبل عضو یک بنگاه فعال هستید.",
                reply_markup=home_keyboard(current),
            )
        return

    with db.connect() as c,c.cursor() as cur:
        cur.execute(
            """SELECT i.*,a.status agency_status
               FROM partner_invites i
               JOIN partner_agencies a ON a.id=i.agency_id
               WHERE i.code=%s
                 AND i.is_revoked=FALSE
                 AND i.used_count<i.max_uses
                 AND (i.expires_at IS NULL OR i.expires_at>NOW())
               FOR UPDATE OF i""",
            (code,),
        )
        row=cur.fetchone()
        if not row or row["agency_status"]!="ACTIVE":
            await update.message.reply_text(
                "کد دعوت معتبر، فعال یا دارای ظرفیت نیست."
            )
            return

        defaults={
            "AGENCY_ADMIN": (True,True,True,True,True,True),
            "ADVISOR": (True,False,True,False,False,False),
            "SECRETARY": (True,False,False,False,False,False),
        }.get(row["role"],(True,False,True,False,False,False))

        cur.execute(
            """INSERT INTO partner_members(
                   agency_id,telegram_user_id,username,display_name,role,
                   can_add_file,can_publish_own,can_request_central,
                   can_manage_brand,can_manage_team,can_view_stats,is_active
               )
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
               ON CONFLICT(agency_id,telegram_user_id)
               DO UPDATE SET
                   username=EXCLUDED.username,
                   display_name=EXCLUDED.display_name,
                   role=EXCLUDED.role,
                   can_add_file=EXCLUDED.can_add_file,
                   can_publish_own=EXCLUDED.can_publish_own,
                   can_request_central=EXCLUDED.can_request_central,
                   can_manage_brand=EXCLUDED.can_manage_brand,
                   can_manage_team=EXCLUDED.can_manage_team,
                   can_view_stats=EXCLUDED.can_view_stats,
                   is_active=TRUE,
                   updated_at=NOW()
               RETURNING id""",
            (
                row["agency_id"],u.id,u.username,u.full_name,row["role"],
                *defaults,
            ),
        )
        member_id=cur.fetchone()["id"]
        cur.execute(
            "UPDATE partner_invites SET used_count=used_count+1 WHERE id=%s",
            (row["id"],),
        )
        c.commit()

    context.user_data.clear()
    db.audit(
        "MEMBER_JOINED",
        u.id,
        row["agency_id"],
        "MEMBER",
        member_id,
        {"role":row["role"],"invite_id":row["id"]},
    )
    await update.message.reply_text(
        f"✅ به‌عنوان «{ROLE_LABELS.get(row['role'],row['role'])}» "
        "به تیم بنگاه اضافه شدید.",
        reply_markup=home_keyboard(db.member(u.id)),
    )


async def owner_callback(q,context,a):
    if a=="o_pending":
        rows=db.all_rows("SELECT id,brand_name,city,manager_name FROM partner_agencies WHERE status='PENDING' ORDER BY id")
        kb=[[B(f"{r['brand_name']} — {r['city']}",callback_data=f"o_view_{r['id']}")] for r in rows]+[[B("⬅️ منو",callback_data="home")]]
        await q.edit_message_text("درخواست‌های عضویت:",reply_markup=M(kb)); return
    if a.startswith("o_view_"):
        aid=int(a.rsplit('_',1)[1]); r=db.one("SELECT * FROM partner_agencies WHERE id=%s",(aid,))
        await q.edit_message_text(f"🏢 {r['brand_name']}\nنام رسمی: {r['legal_name']}\nشهر: {r['city']}\nآدرس: {r['address_text']}\nمدیر: {r['manager_name']}\nتماس: {r['manager_phone']}\nجواز: {r['license_number']}",reply_markup=M([[B("✅ تأیید",callback_data=f"o_approve_{aid}"),B("❌ رد",callback_data=f"o_reject_{aid}")],[B("⬅️",callback_data="o_pending")]])); return
    if a.startswith("o_approve_") or a.startswith("o_reject_"):
        aid=int(a.rsplit('_',1)[1]); approved=a.startswith("o_approve_"); status="ACTIVE" if approved else "REJECTED"
        db.execute("UPDATE partner_agencies SET status=%s,verified_at=CASE WHEN %s THEN NOW() ELSE verified_at END,verified_by_telegram_id=%s,updated_at=NOW() WHERE id=%s",(status,approved,OWNER_ID,aid))
        if not approved: db.execute("UPDATE partner_members SET is_active=FALSE,updated_at=NOW() WHERE agency_id=%s",(aid,))
        row=db.one("SELECT telegram_user_id FROM partner_members WHERE agency_id=%s AND role='AGENCY_OWNER' ORDER BY id LIMIT 1",(aid,))
        if row:
            try: await context.bot.send_message(row['telegram_user_id'],"✅ بنگاه شما تأیید و فعال شد." if approved else "❌ درخواست بنگاه تأیید نشد.")
            except TelegramError: pass
        db.audit("AGENCY_APPROVED" if approved else "AGENCY_REJECTED",OWNER_ID,aid,"AGENCY",aid)
        await q.edit_message_text("انجام شد.",reply_markup=M([[B("⬅️ درخواست‌ها",callback_data="o_pending")]])); return
    if a=="o_submissions":
        rows=db.all_rows("""SELECT s.id,s.tracking_code,a.brand_name,s.district,s.area_m2 FROM partner_submissions s JOIN partner_agencies a ON a.id=s.agency_id WHERE s.status IN('SUBMITTED','UNDER_REVIEW') ORDER BY s.id LIMIT 40""")
        kb=[[B(f"{r['tracking_code']} | {r['brand_name']} | {r['district']}",callback_data=f"o_sub_{r['id']}")] for r in rows]+[[B("⬅️ منو",callback_data="home")]]
        await q.edit_message_text("صف بررسی فایل‌ها:",reply_markup=M(kb)); return
    if a.startswith("o_sub_"):
        sid=int(a.rsplit('_',1)[1]); s=db.one("SELECT s.*,a.brand_name FROM partner_submissions s JOIN partner_agencies a ON a.id=s.agency_id WHERE s.id=%s",(sid,))
        duplicate=f"\n⚠️ شباهت: {s['duplicate_score']}٪ با Submission #{s['duplicate_of_submission_id']}" if s.get('duplicate_of_submission_id') else ""
        text=f"🧾 {s['tracking_code']}\nبنگاه: {s['brand_name']}\nمنطقه: {s['district']}\nآدرس: {s['address_text']}\nمتراژ: {s['area_m2']}\nمالک: {s['owner_name']}\nشماره خصوصی: {s['owner_phone']}{duplicate}"
        await q.edit_message_text(text,reply_markup=M([[B("✅ تأیید و صف مرکزی",callback_data=f"o_central_{sid}")],[B("❌ رد",callback_data=f"o_subreject_{sid}")],[B("⬅️ صف",callback_data="o_submissions")]])); return
    if a.startswith("o_central_"):
        sid=int(a.rsplit('_',1)[1])
        text=("✅ تأیید شد؛ فایل با عکس‌هایش به صف کانال مرکزی می‌رود." if central_queued(sid)
              else "⚠️ فقط فایل فروشی با قیمت به کانال مرکزی می‌رود.")
        await q.edit_message_text(text,reply_markup=M([[B("⬅️ صف",callback_data="o_submissions")]])); return
    if a.startswith("o_subreject_"):
        sid=int(a.rsplit('_',1)[1]); db.execute("UPDATE partner_submissions SET status='REJECTED',updated_at=NOW() WHERE id=%s",(sid,)); db.audit("SUBMISSION_REJECTED",OWNER_ID,None,"SUBMISSION",sid)
        await q.edit_message_text("فایل رد شد.",reply_markup=M([[B("⬅️ صف",callback_data="o_submissions")]])); return
    if a=="o_stats":
        r=db.one("""SELECT (SELECT count(*) FROM partner_agencies WHERE status='ACTIVE') agencies,(SELECT count(*) FROM partner_members WHERE is_active) members,(SELECT count(*) FROM partner_submissions) files,(SELECT count(*) FROM partner_submissions WHERE status='ACTIVE') active""")
        await q.edit_message_text(f"📊 آمار شبکه\n\nبنگاه فعال: {r['agencies']}\nکاربر: {r['members']}\nکل فایل: {r['files']}\nفایل فعال: {r['active']}",reply_markup=M([[B("⬅️",callback_data="home")]]))


async def begin_property(q,context):
    context.user_data.clear(); context.user_data.update(flow="property",step=0,data={"photos":[]})
    await q.edit_message_text("نوع ملک را انتخاب کنید:",reply_markup=M([[B(x,callback_data=f"ptype_{v}")] for x,v in TYPE_KEYS.items()]+[[B("❌ انصراف",callback_data="home")]]))


def field_prompt(st):
    key,label,opt=FIELDS[st['step']]
    kb=[]
    if opt: kb.append([B("⏭ رد کردن",callback_data="pf_skip")])
    kb.append([B("❌ انصراف",callback_data="home")])
    return f"مرحله {st['step']+1} از {len(FIELDS)}\n{label} را وارد کنید:",M(kb)


FORM_CALLBACKS=("pf_skip","pf_photos_done","pf_no_photos","pf_submit")
SAVED_FILE_CALLBACKS=("pf_own_now","pf_own_schedule","pf_own_slot","pf_central")


async def property_callback(q,context,a,mem):
    st=context.user_data
    # A restart empties the form; an old button must not crash the bot.
    stale_form=(a.startswith("ptype_") or a.startswith("deal_") or a in FORM_CALLBACKS) and "data" not in st
    stale_file=a in SAVED_FILE_CALLBACKS and not st.get("saved_submission_id")
    if stale_form or stale_file:
        context.user_data.clear()
        await q.edit_message_text("⌛️ این فرم منقضی شده (ربات به‌روز شد).\nلطفاً دوباره از «➕ ثبت فایل جدید» شروع کنید؛ فایل‌هایی که قبلاً ثبت شدن توی «🏠 فایل‌های من» هستن.",reply_markup=home_keyboard(mem)); return
    if a.startswith("ptype_"):
        st['data']['property_type']=a.replace("ptype_",""); st['step']=1
        await q.edit_message_text("نوع معامله:",reply_markup=M([[B(x,callback_data=f"deal_{v}")] for x,v in DEAL_KEYS.items()])); return
    if a.startswith("deal_"):
        st['data']['deal_type']=a.replace("deal_",""); st['step']=2; text,kb=field_prompt(st); await q.edit_message_text(text,reply_markup=kb); return
    if a=="pf_skip":
        key,_,opt=FIELDS[st['step']]
        if not opt: await q.answer("این فیلد الزامی است.",show_alert=True); return
        st['data'][key]=SKIP; st['step']+=1
        if st['step']>=len(FIELDS): await photos_prompt(q,context)
        else: text,kb=field_prompt(st); await q.edit_message_text(text,reply_markup=kb)
        return
    if a in ("pf_photos_done","pf_no_photos"):
        if a=="pf_no_photos": st['data']['photos']=[]
        await preview(q,context); return
    if a=="pf_submit":
        if not mem.get("can_add_file"):
            await q.answer("دسترسی ثبت فایل ندارید.",show_alert=True); return
        await save_submission(q,context,mem); return
    if a=="pf_own_now":
        if not mem.get("can_publish_own"):
            await q.answer("دسترسی انتشار ندارید.",show_alert=True); return
        await publish_own(q,context,mem); return
    if a=="pf_own_slot":
        if not mem.get("can_publish_own"):
            await q.answer("دسترسی زمان‌بندی ندارید.",show_alert=True); return
        ch=ct.default_channel(mem['agency_id'])
        if not ch: await q.answer("ابتدا کانال بنگاه را متصل کنید.",show_alert=True); return
        ct.cancel_queued(st['saved_submission_id'])
        planned=ct.queue_files(mem['agency_id'],[st['saved_submission_id']],ch['id'])
        db.audit("AGENCY_PUBLICATION_SCHEDULED",q.from_user.id,mem['agency_id'],"SUBMISSION",st['saved_submission_id'],{"scheduled_for":planned[0][1].isoformat()})
        await q.edit_message_text(f"✅ فایل در نوبت کانال قرار گرفت: {ct.when_label(planned[0][1])}",reply_markup=M([[B("⬅️ منو",callback_data="home")]])); return
    if a=="pf_own_schedule":
        if not mem.get("can_publish_own"):
            await q.answer("دسترسی زمان‌بندی ندارید.",show_alert=True); return
        context.user_data['flow']='schedule'
        await q.edit_message_text("زمان انتشار را بفرستید.\nمثال: فردا ۱۰:۳۰\nیا: ۱۴۰۵/۰۷/۱۸ ۱۸:۰۰")
        return
    if a=="pf_central":
        if not mem.get("can_request_central"):
            await q.answer("دسترسی ارسال به کانال مرکزی ندارید.",show_alert=True); return
        sid=st.get('saved_submission_id')
        text=("✅ فایل در صف کانال مرکزی است." if central_queued(sid)
              else "⚠️ فقط فایل فروشی با قیمت به کانال مرکزی می‌رود.")
        await q.edit_message_text(text,reply_markup=M([[B("⬅️ منو",callback_data="home")]])); return
    if a=="pf_done": context.user_data.clear(); await q.edit_message_text("فایل ذخیره شد.",reply_markup=home_keyboard(mem)); return


async def property_text(update,context):
    st=context.user_data; key,label,opt=FIELDS[st['step']]; raw=update.message.text.strip()
    try:
        if key in ("area_m2",): value=float(digits(raw).replace(",","."))
        elif key in ("bedrooms","total_floors","units_per_floor","build_year"): value=int_value(raw,True)
        elif key in ("current_price_toman","deposit_toman","rent_toman"): value=price(raw)
        elif key=="owner_phone": value=clean_phone(raw)
        else:value=raw
    except Exception:
        await update.message.reply_text(f"مقدار «{label}» معتبر نیست. دوباره ارسال کنید."); return
    st['data'][key]=value; st['step']+=1
    if st['step']>=len(FIELDS): await update.message.reply_text("📸 عکس‌ها را ارسال کنید.",reply_markup=M([[B("✅ پایان عکس‌ها",callback_data="pf_photos_done"),B("بدون عکس",callback_data="pf_no_photos")],[B("❌ انصراف",callback_data="home")]])); st['phase']="photos"
    else:
        text,kb=field_prompt(st); await update.message.reply_text(text,reply_markup=kb)


async def photos_prompt(q,context):
    context.user_data['phase']="photos"; await q.edit_message_text("📸 عکس‌ها را ارسال کنید.",reply_markup=M([[B("✅ پایان عکس‌ها",callback_data="pf_photos_done"),B("بدون عکس",callback_data="pf_no_photos")]]))


async def handle_photo(update,context):
    scoped=db.bot_agency(context.bot.id); mem_scope=db.member(update.effective_user.id)
    if scoped and (not mem_scope or mem_scope['agency_id']!=scoped['id']): return
    if context.user_data.get("flow")=="brand_logo":
        mem=db.member(update.effective_user.id)
        if not mem or not mem['can_manage_brand']: return
        p=update.message.photo[-1]
        db.execute("UPDATE partner_brand_settings SET logo_file_id=%s,updated_at=NOW() WHERE agency_id=%s",(p.file_id,mem['agency_id']))
        db.audit("BRAND_LOGO_UPDATED",update.effective_user.id,mem['agency_id'],"BRAND",mem['agency_id'])
        context.user_data.clear(); await update.message.reply_text("✅ لوگوی بنگاه ذخیره شد.",reply_markup=home_keyboard(mem)); return
    if context.user_data.get("flow")!="property" or context.user_data.get("phase")!="photos": return
    p=update.message.photo[-1]; arr=context.user_data['data']['photos']
    if p.file_unique_id not in {x['unique_id'] for x in arr}: arr.append({"file_id":p.file_id,"unique_id":p.file_unique_id})
    await update.message.reply_text(f"✅ عکس دریافت شد؛ مجموع {len(arr)}",reply_markup=M([[B("✅ پایان عکس‌ها",callback_data="pf_photos_done")]]))


async def preview(q,context):
    d=context.user_data['data']; private="✅ ثبت شده" if d.get('owner_phone') else "❌ ناقص"
    text=f"🧾 پیش‌نمایش\n\nمنطقه: {d.get('district')}\nآدرس: {d.get('address_text')}\nمتراژ: {d.get('area_m2')}\nخواب: {d.get('bedrooms',SKIP)}\nقیمت: {d.get('current_price_toman',SKIP)}\nعکس: {len(d.get('photos',[]))}\nاطلاعات مالک: {private}\n\n🔒 نام، شماره و آدرس خصوصی مالک در خروجی عمومی نمایش داده نمی‌شود."
    await q.edit_message_text(text,reply_markup=M([[B("✅ ثبت نهایی",callback_data="pf_submit")],[B("❌ انصراف",callback_data="home")]]))


async def save_submission(q,context,mem):
    d={k:(None if v==SKIP else v) for k,v in context.user_data['data'].items()}; code=db.tracking_code(mem['agency_id'])
    cols=[k for k,_,_ in FIELDS]
    duplicate=db.one("""SELECT id,tracking_code FROM partner_submissions
      WHERE agency_id=%s AND owner_phone=%s AND district=%s
        AND ABS(area_m2-%s)<=2 AND status NOT IN('REJECTED','INACTIVE')
        AND created_at>NOW()-interval '180 days' ORDER BY id DESC LIMIT 1""",
      (mem['agency_id'],d.get('owner_phone'),d.get('district'),d.get('area_m2')))
    with db.connect() as c,c.cursor() as cur:
        cur.execute(f"INSERT INTO partner_submissions(tracking_code,agency_id,submitted_by_member_id,{','.join(cols)},status) VALUES(%s,%s,%s,{','.join(['%s']*len(cols))},'DRAFT') RETURNING id",[code,mem['agency_id'],mem['id']]+[d.get(k) for k in cols])
        sid=cur.fetchone()['id']
        if duplicate:
            cur.execute("UPDATE partner_submissions SET duplicate_score=95,duplicate_of_submission_id=%s WHERE id=%s",(duplicate['id'],sid))
        for pos,p in enumerate(d.get('photos',[]),1): cur.execute("INSERT INTO partner_submission_media(submission_id,telegram_file_id,telegram_file_unique_id,position) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",(sid,p['file_id'],p['unique_id'],pos))
        c.commit()
    context.user_data['saved_submission_id']=sid; db.audit("SUBMISSION_CREATED",q.from_user.id,mem['agency_id'],"SUBMISSION",sid,{"tracking_code":code})
    own=db.one("SELECT id FROM partner_channel_connections WHERE agency_id=%s AND is_active AND is_default LIMIT 1",(mem['agency_id'],))
    rows=[]
    auto=""
    if own and mem['can_publish_own']:
        rows.append([B("🚀 انتشار فوری",callback_data="pf_own_now"),B("🗓 زمان‌بندی",callback_data="pf_own_schedule")])
        slot=ct.plan_times(mem['agency_id'],1)
        rows.append([B(f"⏰ نوبت بعدی کانال ({ct.when_label(slot[0])})",callback_data="pf_own_slot")])
    if own and ct.agency_bot(mem['agency_id']):
        auto="\n⏰ اگه انتخابی نکنید، چند دقیقهٔ دیگه خودکار توی نوبت کانال بنگاه قرار می‌گیره."
    if central.queue(sid):
        db.audit("CENTRAL_QUEUED",q.from_user.id,mem['agency_id'],"SUBMISSION",sid)
        auto+="\n🌐 برای کانال مرکزی هم خودکار در صف انتشار قرار گرفت."
    rows += [[B("✅ فعلاً تمام",callback_data="pf_done")]]
    warning=f"\n\n⚠️ مشابه فایل {duplicate['tracking_code']} تشخیص داده شد؛ مدیر مرکزی هنگام بررسی آن را می‌بیند." if duplicate else ""
    await q.edit_message_text(f"✅ فایل ثبت شد.\nکد پیگیری: {code}{warning}\n\nمقصد انتشار را انتخاب کنید:{auto}",reply_markup=M(rows))


async def publish_own(q,context,mem):
    sid=context.user_data['saved_submission_id']; s=db.one("SELECT * FROM partner_submissions WHERE id=%s AND agency_id=%s",(sid,mem['agency_id']))
    conn=ct.default_channel(mem['agency_id'])
    if not conn or not ct.agency_bot(mem['agency_id']): await q.answer("ابتدا ربات و کانال بنگاه را متصل کنید.",show_alert=True); return
    ct.cancel_queued(sid)
    try:
        ids=await ct.publish_submission(s,conn,[context.bot])
        ct.record_publication(sid,conn['id'],ids)
        db.audit("AGENCY_CHANNEL_PUBLISHED",q.from_user.id,mem['agency_id'],"SUBMISSION",sid,{"message_id":ids[0]})
        await q.edit_message_text("✅ با برند بنگاه در کانال خودتان منتشر شد.",reply_markup=M([[B("⬅️ منو",callback_data="home")]]))
    except Exception as exc:
        db.audit("AGENCY_PUBLISH_FAILED",q.from_user.id,mem['agency_id'],"SUBMISSION",sid,{"error":str(exc)})
        await q.edit_message_text("❌ انتشار انجام نشد. دسترسی ادمین ربات به کانال و شناسه کانال را بررسی کنید.\n"+str(exc)[:500],reply_markup=M([[B("⬅️ اتصال‌ها",callback_data="p_connect")]]))


async def schedule_text(update,context):
    mem=db.member(update.effective_user.id)
    if not mem or not mem.get("can_publish_own"):
        context.user_data.clear()
        await update.message.reply_text("دسترسی زمان‌بندی انتشار ندارید.")
        return
    raw=digits(update.message.text.strip()); now=datetime.now(TEHRAN)
    try:
        m=re.search(r"(\d{1,2})[:٫.](\d{2})",raw)
        if not m: raise ValueError("time")
        hour,minute=int(m.group(1)),int(m.group(2))
        if "فردا" in raw:
            local=(now+timedelta(days=1)).replace(hour=hour,minute=minute,second=0,microsecond=0)
        else:
            dm=re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})",raw)
            if not dm: raise ValueError("date")
            year,month,day=map(int,dm.groups())
            if year<1700:
                try:
                    import jdatetime
                    g=jdatetime.date(year,month,day).togregorian(); year,month,day=g.year,g.month,g.day
                except Exception: raise ValueError("jalali")
            local=datetime(year,month,day,hour,minute,tzinfo=TEHRAN)
        if local<=now: raise ValueError("past")
        sid=context.user_data['saved_submission_id']; ch=db.one("SELECT id FROM partner_channel_connections WHERE agency_id=%s AND is_default AND is_active",(mem['agency_id'],))
        if not ch: raise ValueError("channel")
        ct.cancel_queued(sid)
        db.execute("INSERT INTO partner_publication_jobs(submission_id,target_type,channel_connection_id,status,scheduled_for) VALUES(%s,'AGENCY_CHANNEL',%s,'SCHEDULED',%s)",(sid,ch['id'],local))
        db.audit("AGENCY_PUBLICATION_SCHEDULED",update.effective_user.id,mem['agency_id'],"SUBMISSION",sid,{"scheduled_for":local.isoformat()})
        context.user_data.clear(); await update.message.reply_text(f"✅ انتشار برای {local.strftime('%Y-%m-%d %H:%M')} زمان‌بندی شد.",reply_markup=home_keyboard(mem))
    except Exception:
        await update.message.reply_text("زمان قابل تشخیص نیست یا در گذشته است.\nمثال دقیق: فردا ۱۰:۳۰")


def can_publish(mem):
    return bool(mem) and (mem["role"] in ("AGENCY_OWNER","AGENCY_ADMIN") or bool(mem.get("can_publish_own")))


def area_label(value):
    try: return fa(int(float(value)))
    except (TypeError, ValueError): return fa(value or "-")


async def list_files(q,mem,page=0):
    rows=db.all_rows("""SELECT id,tracking_code,district,area_m2,status FROM partner_submissions
                        WHERE agency_id=%s AND status<>'DELETED' ORDER BY id DESC LIMIT 11 OFFSET %s""",(mem['agency_id'],page*10))
    kb=[[B(f"{r['tracking_code']} | {r['district']} | {area_label(r['area_m2'])}م | {ct.STATUS_LABELS.get(r['status'],r['status'])}",callback_data=f"pfile_{r['id']}")] for r in rows[:10]]
    nav=[]
    if page: nav.append(B("⬅️ قبلی",callback_data=f"p_files_{page-1}"))
    if len(rows)>10: nav.append(B("بعدی ➡️",callback_data=f"p_files_{page+1}"))
    if nav: kb.append(nav)
    if can_publish(mem) and ct.default_channel(mem['agency_id']) and ct.agency_bot(mem['agency_id']):
        pending=len(ct.unpublished_files(mem['agency_id']))
        if pending: kb.append([B(f"📤 ارسال {fa(pending)} فایل منتشرنشده به کانال",callback_data="pback")])
    kb.append([B("⬅️ منو",callback_data="home")])
    text="🏠 فایل‌های بنگاه\n\nروی هر فایل بزنید تا جزئیات، ارسال به کانال یا حذفش رو ببینید." if rows else "🏠 فایل‌های بنگاه\n\nفایلی ثبت نشده."
    await q.edit_message_text(text,reply_markup=M(kb))


def own_file(mem,sid):
    s=db.one("SELECT * FROM partner_submissions WHERE id=%s AND agency_id=%s",(sid,mem['agency_id']))
    return s if s and s['status']!='DELETED' else None


async def show_file(q,mem,sid,note=None):
    s=own_file(mem,sid)
    if not s: await q.edit_message_text("این فایل پیدا نشد.",reply_markup=M([[B("⬅️ فایل‌ها",callback_data="p_files")]])); return
    state,when=ct.channel_state(sid)
    channel={"published":"✅ منتشر شده","scheduled":f"🗓 در نوبت: {ct.when_label(when)}" if when else "🗓 در نوبت"}.get(state,"❌ هنوز منتشر نشده")
    text=(f"🧾 {s['tracking_code']}\n📍 {s['district']} | {area_label(s['area_m2'])} متر\n💰 {money(s.get('current_price_toman'))}\n"
          f"وضعیت: {ct.STATUS_LABELS.get(s['status'],s['status'])}\nکانال بنگاه: {channel}")
    if note: text=f"{note}\n\n{text}"
    kb=[]
    if state is None and s['status'] in ct.PUBLISHABLE_STATUSES and can_publish(mem):
        slot=ct.plan_times(mem['agency_id'],1)[0]
        kb.append([B("🚀 انتشار فوری",callback_data=f"pfsend_{sid}"),B(f"⏰ نوبت بعدی ({ct.when_label(slot)})",callback_data=f"pfslot_{sid}")])
    if availability_authorized(mem,s):
        kb.append([B("🗑 حذف فایل",callback_data=f"pdel_{sid}")])
    kb.append([B("⬅️ فایل‌ها",callback_data="p_files")])
    await q.edit_message_text(text,reply_markup=M(kb))


async def send_file_to_channel(q,mem,sid,queue=False):
    s=own_file(mem,sid); ch=ct.default_channel(mem['agency_id'])
    if not s or not can_publish(mem): await q.answer("اجازه ارسال این فایل را ندارید.",show_alert=True); return
    if not ch or not ct.agency_bot(mem['agency_id']): await q.answer("ابتدا ربات و کانال بنگاه را متصل کنید.",show_alert=True); return
    if ct.channel_state(sid)[0]: await show_file(q,mem,sid); return
    if queue:
        planned=ct.queue_files(mem['agency_id'],[sid],ch['id'])
        db.audit("AGENCY_PUBLICATION_SCHEDULED",q.from_user.id,mem['agency_id'],"SUBMISSION",sid,{"scheduled_for":planned[0][1].isoformat()})
        await show_file(q,mem,sid,f"✅ در نوبت کانال قرار گرفت: {ct.when_label(planned[0][1])}"); return
    try:
        ids=await ct.publish_submission(s,ch)
        ct.record_publication(sid,ch['id'],ids)
        db.audit("AGENCY_CHANNEL_PUBLISHED",q.from_user.id,mem['agency_id'],"SUBMISSION",sid,{"message_id":ids[0]})
        await show_file(q,mem,sid,"✅ در کانال بنگاه منتشر شد.")
    except Exception as exc:
        db.audit("AGENCY_PUBLISH_FAILED",q.from_user.id,mem['agency_id'],"SUBMISSION",sid,{"error":str(exc)})
        await show_file(q,mem,sid,"❌ انتشار انجام نشد: "+str(exc)[:300])


async def delete_file(q,mem,a):
    sid=int(a.rsplit("_",1)[1]); s=own_file(mem,sid)
    if not s or not availability_authorized(mem,s):
        await q.answer("اجازه حذف این فایل را ندارید.",show_alert=True); return
    if a.startswith("pdel_"):
        await q.edit_message_text(
            f"⚠️ فایل {s['tracking_code']} حذف بشه؟\n\nاز فهرست فایل‌ها برداشته می‌شه و پستش از کانال بنگاه پاک می‌شه. "
            "اگه تلگرام اجازهٔ پاک کردن نده (پست‌های قدیمی‌تر از ۴۸ ساعت)، زیر پست «این فایل دیگر موجود نیست» می‌خوره.",
            reply_markup=M([[B("🗑 آره، حذف کن",callback_data=f"pdelok_{sid}")],[B("↩️ نه",callback_data=f"pfile_{sid}")]])); return
    result=await ct.delete_submission(s)
    db.audit("SUBMISSION_DELETED",q.from_user.id,mem['agency_id'],"SUBMISSION",sid,result)
    note="" if not result["kept_posts"] else "\nپست کانال قدیمی بود و پاک نشد؛ زیرش «دیگر موجود نیست» خورد."
    await q.edit_message_text(f"✅ فایل {s['tracking_code']} حذف شد.{note}",reply_markup=M([[B("⬅️ فایل‌ها",callback_data="p_files")]]))


async def backfill_files(q,mem,confirmed):
    ch=ct.default_channel(mem['agency_id'])
    if not can_publish(mem) or not ch or not ct.agency_bot(mem['agency_id']):
        await q.answer("ابتدا ربات و کانال بنگاه را متصل کنید.",show_alert=True); return
    files=ct.unpublished_files(mem['agency_id'])
    if not files: await list_files(q,mem); return
    if not confirmed:
        times=ct.plan_times(mem['agency_id'],len(files))
        how=f"طبق زمان‌بندی کانال، از {ct.when_label(times[0])} هر نوبت یکی ارسال می‌شه."
        await q.edit_message_text(f"📤 {fa(len(files))} فایل هنوز توی کانال بنگاه منتشر نشده.\n\n{how}",
                                  reply_markup=M([[B("✅ ارسال کن",callback_data="pbackok")],[B("⬅️ فایل‌ها",callback_data="p_files")]])); return
    planned=ct.queue_files(mem['agency_id'],[f['id'] for f in files],ch['id'])
    db.audit("AGENCY_BACKFILL_SCHEDULED",q.from_user.id,mem['agency_id'],"AGENCY",mem['agency_id'],{"count":len(planned)})
    last=ct.when_label(planned[-1][1])
    await q.edit_message_text(f"✅ {fa(len(planned))} فایل در نوبت کانال قرار گرفت؛ اولی {ct.when_label(planned[0][1])} و آخری {last}.",
                              reply_markup=M([[B("⬅️ فایل‌ها",callback_data="p_files")]]))


def schedule_text_view(agency_id):
    settings=ct.schedule_settings(agency_id)
    if settings["slot_times"]:
        times="، ".join(fa(t) for t in settings["slot_times"])
    else:
        default=ct.default_slot_times(datetime.now(TEHRAN).weekday())
        times="پیش‌فرض، هماهنگ با کانال شاه‌فایل"+(f" ({'، '.join(fa(t) for t in default)})" if default else "")
    queued=db.one("""SELECT count(*) AS n, min(j.scheduled_for) AS first FROM partner_publication_jobs j
                     JOIN partner_submissions s ON s.id=j.submission_id
                     WHERE s.agency_id=%s AND j.target_type='AGENCY_CHANNEL' AND j.status='SCHEDULED'""",(agency_id,))
    lines=["⏰ زمان‌بندی ارسال به کانال","",f"ساعت‌های ارسال: {times}",
           f"وضعیت: {'✅ فعال' if settings['is_active'] else '⏸ متوقف'}",
           f"در صف: {fa(queued['n'])} فایل"+(f" (بعدی: {ct.when_label(queued['first'])})" if queued['n'] and queued['first'] else ""),"",
           "فایل‌هایی که به نوبت کانال می‌رن، هر کدوم توی اولین ساعت خالی منتشر می‌شن (هر ساعت یه فایل)."]
    rows=[[B("✏️ تنظیم ساعت‌ها",callback_data="psched_set")],
          [B("⏸ توقف ارسال" if settings['is_active'] else "▶️ ادامهٔ ارسال",callback_data="psched_toggle")]]
    if settings["slot_times"]: rows.append([B("↩️ برگشت به زمان‌بندی پیش‌فرض",callback_data="psched_clear")])
    rows.append([B("⬅️ منو",callback_data="home")])
    return "\n".join(lines),M(rows)


async def schedule_callback(q,context,a,mem):
    if not can_publish(mem): await q.answer("دسترسی زمان‌بندی ندارید.",show_alert=True); return
    aid=mem['agency_id']
    if a=="psched_set":
        context.user_data.clear(); context.user_data['flow']="slot_times"
        await q.edit_message_text("ساعت‌های ارسال به کانال رو بفرستید، مثلاً:\n۱۰ ۱۴ ۱۸:۳۰ ۲۱\n\nهر روز توی همین ساعت‌ها، هر بار یه فایل از نوبت کانال منتشر می‌شه.",
                                  reply_markup=M([[B("⬅️ انصراف",callback_data="p_sched")]])); return
    if a=="psched_toggle":
        settings=ct.schedule_settings(aid); active=not settings['is_active']; ct.save_schedule(aid,is_active=active)
        if active: ct.reslot_overdue(aid)
        db.audit("AGENCY_SCHEDULE_TOGGLED",q.from_user.id,aid,"AGENCY",aid,{"is_active":active})
    if a=="psched_clear":
        ct.save_schedule(aid,slot_times=[]); db.audit("AGENCY_SCHEDULE_CLEARED",q.from_user.id,aid,"AGENCY",aid)
    text,kb=schedule_text_view(aid)
    await q.edit_message_text(text,reply_markup=kb)


async def slot_times_text(update,context):
    mem=db.member(update.effective_user.id)
    if not can_publish(mem): context.user_data.clear(); await update.message.reply_text("دسترسی زمان‌بندی ندارید."); return
    times=ct.parse_slot_times(update.message.text)
    if not times:
        await update.message.reply_text("ساعت‌ها رو متوجه نشدم 🙏 مثلاً بفرستید: ۱۰ ۱۴ ۱۸:۳۰ ۲۱"); return
    ct.save_schedule(mem['agency_id'],slot_times=times); context.user_data.clear()
    db.audit("AGENCY_SCHEDULE_SET",update.effective_user.id,mem['agency_id'],"AGENCY",mem['agency_id'],{"slot_times":times})
    text,kb=schedule_text_view(mem['agency_id'])
    await update.message.reply_text("✅ ساعت‌ها ذخیره شد.\n\n"+text,reply_markup=kb)


async def brand_menu(q,mem):
    b=db.brand(mem['agency_id']); text=f"🎨 برند وایت‌لیبل\n\nنام: {b.get('display_name')}\nتلفن: {b.get('public_phone') or '-'}\nموبایل: {b.get('public_mobile') or '-'}\nتلگرام: {b.get('telegram_username') or '-'}\nلوگو: {'✅' if b.get('logo_file_id') else '❌'}"
    await q.edit_message_text(text,reply_markup=M([[B("نام برند",callback_data="brand_display_name"),B("شماره عمومی",callback_data="brand_public_mobile")],[B("آیدی تلگرام",callback_data="brand_telegram_username"),B("فوتر",callback_data="brand_footer_text")],[B("CTA",callback_data="brand_cta_text"),B("🖼 لوگو",callback_data="brand_logo")],[B("⬅️",callback_data="home")]]))


async def brand_callback(q,context,a,mem):
    if not mem['can_manage_brand']: await q.answer("دسترسی مدیریت برند ندارید.",show_alert=True); return
    field=a.replace("brand_","")
    if field=="logo":
        context.user_data.clear(); context.user_data['flow']='brand_logo'; await q.edit_message_text("لوگوی بنگاه را به‌صورت عکس ارسال کنید:"); return
    allowed={"display_name","public_mobile","telegram_username","footer_text","cta_text"}
    if field not in allowed:return
    context.user_data.clear(); context.user_data.update(flow="brand",field=field)
    await q.edit_message_text("مقدار جدید را ارسال کنید:")


async def brand_text(update,context):
    mem=db.member(update.effective_user.id); field=context.user_data['field']; value=update.message.text.strip()
    if field=="public_mobile": value=clean_phone(value)
    db.execute(f"UPDATE partner_brand_settings SET {field}=%s,updated_at=NOW() WHERE agency_id=%s",(value,mem['agency_id']))
    db.audit("BRAND_UPDATED",update.effective_user.id,mem['agency_id'],"BRAND",mem['agency_id'],{"field":field}); context.user_data.clear()
    await update.message.reply_text("✅ برند به‌روزرسانی شد.",reply_markup=home_keyboard(mem))


async def connection_menu(q,mem):
    bot=db.one("SELECT bot_username,status FROM partner_bot_connections WHERE agency_id=%s",(mem['agency_id'],)); ch=db.one("SELECT channel_title,channel_username,telegram_channel_id FROM partner_channel_connections WHERE agency_id=%s AND is_default",(mem['agency_id'],))
    text=f"🔌 اتصال‌ها\n\nربات اختصاصی: {('@'+bot['bot_username']) if bot else 'متصل نیست'}\nکانال پیش‌فرض: {(ch['channel_title'] or ch['channel_username'] or ch['telegram_channel_id']) if ch else 'متصل نیست'}\n\nتوکن پس از دریافت رمزگذاری می‌شود و متن پیام توکن حذف خواهد شد."
    await q.edit_message_text(text,reply_markup=M([[B("🤖 اتصال ربات اختصاصی",callback_data="conn_bot")],[B("📣 اتصال کانال",callback_data="conn_channel")],[B("🧪 تست اتصال",callback_data="conn_test")],[B("⬅️",callback_data="home")]]))


async def connection_callback(q,context,a,mem):
    if not mem['can_manage_brand']: await q.answer("دسترسی ندارید.",show_alert=True); return
    if a=="conn_bot": context.user_data.clear(); context.user_data['flow']="connect_bot"; await q.edit_message_text("توکن ربات اختصاصی بنگاه را ارسال کنید.\nپس از ثبت، پیام توکن حذف می‌شود."); return
    if a=="conn_channel": context.user_data.clear(); context.user_data['flow']="connect_channel"; await q.edit_message_text("شناسه عددی کانال مثل ‎-100... یا @username کانال را ارسال کنید. ربات اختصاصی باید ادمین کانال باشد."); return
    if a=="conn_test":
        bc=db.one("SELECT * FROM partner_bot_connections WHERE agency_id=%s AND status='ACTIVE'",(mem['agency_id'],)); ch=db.one("SELECT * FROM partner_channel_connections WHERE agency_id=%s AND is_default AND is_active",(mem['agency_id'],))
        if not bc or not ch: await q.answer("ربات یا کانال کامل متصل نیست.",show_alert=True); return
        try: await Bot(decrypt_token(bc['encrypted_token'])).send_message(ch['telegram_channel_id'],"✅ اتصال امن سامانه وایت‌لیبل با موفقیت تأیید شد.")
        except Exception as exc: await q.edit_message_text("❌ تست ناموفق:\n"+str(exc)[:500],reply_markup=M([[B("⬅️",callback_data="p_connect")]])); return
        db.execute("UPDATE partner_channel_connections SET verified_at=NOW() WHERE id=%s",(ch['id'],)); await q.edit_message_text("✅ اتصال ربات و کانال سالم است.",reply_markup=M([[B("⬅️",callback_data="p_connect")]]))


async def connect_bot_text(update,context):
    mem=db.member(update.effective_user.id); token=update.message.text.strip()
    try:
        info=await Bot(token).get_me()
        if info.id==context.bot.id: raise ValueError("توکن ربات Hub را نمی‌توان به‌عنوان ربات اختصاصی ثبت کرد")
        db.execute("""INSERT INTO partner_bot_connections(agency_id,bot_id,bot_username,encrypted_token,token_fingerprint,last_checked_at)
          VALUES(%s,%s,%s,%s,%s,NOW()) ON CONFLICT(agency_id) DO UPDATE SET bot_id=EXCLUDED.bot_id,bot_username=EXCLUDED.bot_username,encrypted_token=EXCLUDED.encrypted_token,token_fingerprint=EXCLUDED.token_fingerprint,status='ACTIVE',last_checked_at=NOW(),updated_at=NOW()""",
          (mem['agency_id'],info.id,info.username,encrypt_token(token),token_fingerprint(token)))
        try: await update.message.delete()
        except TelegramError: pass
        context.user_data.clear(); db.audit("WHITE_LABEL_BOT_CONNECTED",update.effective_user.id,mem['agency_id'],"BOT",info.id)
        await context.bot.send_message(update.effective_chat.id,f"✅ ربات اختصاصی @{info.username} متصل شد. سرویس حداکثر تا یک دقیقه آینده آن را فعال می‌کند.",reply_markup=home_keyboard(mem))
    except Exception as exc: await update.message.reply_text("❌ توکن معتبر نیست یا ربات در دسترس نیست.\n"+str(exc)[:300])


async def connect_channel_text(update,context):
    mem=db.member(update.effective_user.id); raw=update.message.text.strip(); bc=db.one("SELECT * FROM partner_bot_connections WHERE agency_id=%s AND status='ACTIVE'",(mem['agency_id'],))
    if not bc: await update.message.reply_text("ابتدا ربات اختصاصی را متصل کنید."); return
    try:
        target=int(raw) if re.fullmatch(r"-?\d+",raw) else raw
        bot=Bot(decrypt_token(bc['encrypted_token'])); chat=await bot.get_chat(target); me=await bot.get_me(); admin=await bot.get_chat_member(chat.id,me.id)
        if admin.status not in ("administrator","creator"): raise ValueError("ربات ادمین کانال نیست")
        db.execute("UPDATE partner_channel_connections SET is_default=FALSE WHERE agency_id=%s",(mem['agency_id'],))
        db.execute("""INSERT INTO partner_channel_connections(agency_id,telegram_channel_id,channel_username,channel_title,is_default,verified_at)
          VALUES(%s,%s,%s,%s,TRUE,NOW()) ON CONFLICT(agency_id,telegram_channel_id) DO UPDATE SET channel_username=EXCLUDED.channel_username,channel_title=EXCLUDED.channel_title,is_default=TRUE,is_active=TRUE,verified_at=NOW()""",
          (mem['agency_id'],chat.id,chat.username,chat.title))
        context.user_data.clear(); db.audit("AGENCY_CHANNEL_CONNECTED",update.effective_user.id,mem['agency_id'],"CHANNEL",chat.id)
        await update.message.reply_text(f"✅ کانال «{chat.title}» متصل شد.",reply_markup=home_keyboard(mem))
    except Exception as exc: await update.message.reply_text("❌ اتصال کانال ناموفق بود. ربات باید ادمین و دارای اجازه ارسال پیام باشد.\n"+str(exc)[:400])


def can_manage_team(mem):
    return bool(
        mem
        and (
            mem["role"] == "AGENCY_OWNER"
            or mem.get("can_manage_team")
        )
    )


def can_manage_target(actor, target):
    if target["role"] == "AGENCY_OWNER":
        return False
    if actor["role"] == "AGENCY_OWNER":
        return True
    return (
        can_manage_team(actor)
        and target["role"] not in (
            "AGENCY_OWNER",
            "AGENCY_ADMIN",
        )
    )


async def team_menu(q,mem):
    members=db.all_rows(
        """SELECT id,display_name,username,role,is_active
           FROM partner_members
           WHERE agency_id=%s
           ORDER BY is_active DESC,id""",
        (mem["agency_id"],),
    )
    lines=["👥 تیم بنگاه",""]
    for row in members:
        name=row["display_name"] or row["username"] or f"عضو {row['id']}"
        state="✅" if row["is_active"] else "⛔️"
        lines.append(
            f"{state} {name} — {ROLE_LABELS.get(row['role'],row['role'])}"
        )
    buttons=[]
    if can_manage_team(mem):
        buttons.extend([
            [B("➕ ساخت کد دعوت",callback_data="team_invite")],
            [B("⚙️ مدیریت اعضا",callback_data="team_members")],
            [B("🔑 کدهای دعوت",callback_data="team_invites")],
        ])
    buttons.append([B("⬅️ منوی اصلی",callback_data="home")])
    await q.edit_message_text(
        "\n".join(lines),
        reply_markup=M(buttons),
    )


async def team_callback(q,context,a,mem):
    if a=="p_team":
        await team_menu(q,mem); return

    if not can_manage_team(mem):
        await q.answer("دسترسی مدیریت تیم ندارید.",show_alert=True); return

    if a=="team_invite":
        roles=[
            [B("👤 مشاور",callback_data="team_invrole_ADVISOR")],
            [B("🗂 منشی",callback_data="team_invrole_SECRETARY")],
        ]
        if mem["role"]=="AGENCY_OWNER":
            roles.append([B("👔 مدیر",callback_data="team_invrole_AGENCY_ADMIN")])
        roles.append([B("⬅️ تیم",callback_data="p_team")])
        await q.edit_message_text(
            "نقش عضو جدید را انتخاب کنید:",
            reply_markup=M(roles),
        ); return

    if a.startswith("team_invrole_"):
        role=a.replace("team_invrole_","",1)
        allowed={"ADVISOR","SECRETARY"}
        if mem["role"]=="AGENCY_OWNER":
            allowed.add("AGENCY_ADMIN")
        if role not in allowed:
            await q.answer("نقش مجاز نیست.",show_alert=True); return
        await q.edit_message_text(
            f"تعداد استفاده از کد دعوت برای «{ROLE_LABELS[role]}»:",
            reply_markup=M([
                [
                    B("یک‌بار",callback_data=f"team_invuses_{role}_1"),
                    B("۵ نفر",callback_data=f"team_invuses_{role}_5"),
                    B("۲۰ نفر",callback_data=f"team_invuses_{role}_20"),
                ],
                [B("⬅️ انتخاب نقش",callback_data="team_invite")],
            ]),
        ); return

    if a.startswith("team_invuses_"):
        match=re.fullmatch(
            r"team_invuses_(AGENCY_ADMIN|ADVISOR|SECRETARY)_(1|5|20)",
            a,
        )
        if not match:
            await q.answer("درخواست نامعتبر است.",show_alert=True); return
        role,uses=match.group(1),int(match.group(2))
        if role=="AGENCY_ADMIN" and mem["role"]!="AGENCY_OWNER":
            await q.answer("فقط مالک بنگاه می‌تواند مدیر دعوت کند.",show_alert=True); return
        code=hashlib.sha256(os.urandom(32)).hexdigest()[:10].upper()
        db.execute(
            """INSERT INTO partner_invites(
                   agency_id,code,role,max_uses,expires_at,created_by_member_id
               )
               VALUES(%s,%s,%s,%s,NOW()+interval '7 days',%s)""",
            (mem["agency_id"],code,role,uses,mem["id"]),
        )
        db.audit(
            "TEAM_INVITE_CREATED",
            q.from_user.id,
            mem["agency_id"],
            "INVITE",
            None,
            {"role":role,"max_uses":uses},
        )
        await q.edit_message_text(
            "🔑 کد دعوت ساخته شد\n\n"
            f"<code>{code}</code>\n"
            f"نقش: {ROLE_LABELS[role]}\n"
            f"ظرفیت: {uses} نفر\n"
            "اعتبار: ۷ روز",
            parse_mode=ParseMode.HTML,
            reply_markup=M([[B("⬅️ تیم",callback_data="p_team")]]),
        ); return

    if a=="team_members":
        rows=db.all_rows(
            """SELECT id,display_name,username,role,is_active
               FROM partner_members
               WHERE agency_id=%s
               ORDER BY is_active DESC,id""",
            (mem["agency_id"],),
        )
        buttons=[]
        for row in rows:
            name=row["display_name"] or row["username"] or f"عضو {row['id']}"
            state="✅" if row["is_active"] else "⛔️"
            buttons.append([
                B(
                    f"{state} {name} — {ROLE_LABELS.get(row['role'],row['role'])}",
                    callback_data=f"team_member_{row['id']}",
                )
            ])
        buttons.append([B("⬅️ تیم",callback_data="p_team")])
        await q.edit_message_text(
            "⚙️ مدیریت اعضای بنگاه:",
            reply_markup=M(buttons),
        ); return

    if a.startswith("team_member_"):
        member_id=int(a.rsplit("_",1)[1])
        await show_team_member(q,mem,member_id); return

    if a.startswith("team_toggle_"):
        member_id=int(a.rsplit("_",1)[1])
        target=db.one(
            "SELECT * FROM partner_members WHERE id=%s AND agency_id=%s",
            (member_id,mem["agency_id"]),
        )
        if not target or not can_manage_target(mem,target):
            await q.answer("اجازه تغییر این عضو را ندارید.",show_alert=True); return
        new_state=not target["is_active"]
        db.execute(
            "UPDATE partner_members SET is_active=%s,updated_at=NOW() WHERE id=%s",
            (new_state,member_id),
        )
        db.audit(
            "TEAM_MEMBER_ACTIVATED" if new_state else "TEAM_MEMBER_DEACTIVATED",
            q.from_user.id,
            mem["agency_id"],
            "MEMBER",
            member_id,
        )
        await show_team_member(q,mem,member_id); return

    if a.startswith("team_setrole_"):
        match=re.fullmatch(
            r"team_setrole_(\d+)_(AGENCY_ADMIN|ADVISOR|SECRETARY)",
            a,
        )
        if not match:
            await q.answer("درخواست نامعتبر است.",show_alert=True); return
        member_id,role=int(match.group(1)),match.group(2)
        target=db.one(
            "SELECT * FROM partner_members WHERE id=%s AND agency_id=%s",
            (member_id,mem["agency_id"]),
        )
        if not target or not can_manage_target(mem,target):
            await q.answer("اجازه تغییر نقش این عضو را ندارید.",show_alert=True); return
        if role=="AGENCY_ADMIN" and mem["role"]!="AGENCY_OWNER":
            await q.answer("فقط مالک بنگاه می‌تواند مدیر تعیین کند.",show_alert=True); return
        defaults={
            "AGENCY_ADMIN": (True,True,True,True,True,True),
            "ADVISOR": (True,False,True,False,False,False),
            "SECRETARY": (True,False,False,False,False,False),
        }[role]
        db.execute(
            """UPDATE partner_members
               SET role=%s,can_add_file=%s,can_publish_own=%s,
                   can_request_central=%s,can_manage_brand=%s,
                   can_manage_team=%s,can_view_stats=%s,updated_at=NOW()
               WHERE id=%s""",
            (role,*defaults,member_id),
        )
        db.audit(
            "TEAM_MEMBER_ROLE_CHANGED",
            q.from_user.id,
            mem["agency_id"],
            "MEMBER",
            member_id,
            {"role":role},
        )
        await show_team_member(q,mem,member_id); return

    if a.startswith("team_perm_"):
        rest=a.replace("team_perm_","",1)
        member_text,field=rest.split("_",1)
        member_id=int(member_text)
        if field not in TEAM_PERMISSIONS:
            await q.answer("دسترسی نامعتبر است.",show_alert=True); return
        target=db.one(
            "SELECT * FROM partner_members WHERE id=%s AND agency_id=%s",
            (member_id,mem["agency_id"]),
        )
        if not target or not can_manage_target(mem,target):
            await q.answer("اجازه تغییر این عضو را ندارید.",show_alert=True); return
        if field=="can_manage_team" and mem["role"]!="AGENCY_OWNER":
            await q.answer("فقط مالک بنگاه می‌تواند این دسترسی را بدهد.",show_alert=True); return
        new_value=not bool(target.get(field))
        db.execute(
            f"UPDATE partner_members SET {field}=%s,updated_at=NOW() WHERE id=%s",
            (new_value,member_id),
        )
        db.audit(
            "TEAM_PERMISSION_CHANGED",
            q.from_user.id,
            mem["agency_id"],
            "MEMBER",
            member_id,
            {"permission":field,"enabled":new_value},
        )
        await show_team_member(q,mem,member_id); return

    if a=="team_invites":
        rows=db.all_rows(
            """SELECT id,code,role,max_uses,used_count,expires_at
               FROM partner_invites
               WHERE agency_id=%s AND is_revoked=FALSE
                 AND used_count<max_uses
                 AND (expires_at IS NULL OR expires_at>NOW())
               ORDER BY id DESC LIMIT 30""",
            (mem["agency_id"],),
        )
        buttons=[
            [
                B(
                    f"{r['code']} | {ROLE_LABELS.get(r['role'],r['role'])} "
                    f"{r['used_count']}/{r['max_uses']}",
                    callback_data=f"team_revoke_{r['id']}",
                )
            ]
            for r in rows
        ]
        buttons.append([B("⬅️ تیم",callback_data="p_team")])
        await q.edit_message_text(
            "🔑 کدهای دعوت فعال\nبرای لغو، روی کد بزنید:"
            if rows else "کد دعوت فعالی وجود ندارد.",
            reply_markup=M(buttons),
        ); return

    if a.startswith("team_revoke_"):
        invite_id=int(a.rsplit("_",1)[1])
        db.execute(
            """UPDATE partner_invites
               SET is_revoked=TRUE
               WHERE id=%s AND agency_id=%s""",
            (invite_id,mem["agency_id"]),
        )
        db.audit(
            "TEAM_INVITE_REVOKED",
            q.from_user.id,
            mem["agency_id"],
            "INVITE",
            invite_id,
        )
        await q.edit_message_text(
            "✅ کد دعوت لغو شد.",
            reply_markup=M([[B("⬅️ کدها",callback_data="team_invites")]]),
        ); return


async def show_team_member(q,actor,member_id):
    target=db.one(
        "SELECT * FROM partner_members WHERE id=%s AND agency_id=%s",
        (member_id,actor["agency_id"]),
    )
    if not target:
        await q.answer("عضو پیدا نشد.",show_alert=True); return
    name=target["display_name"] or target["username"] or f"عضو {member_id}"
    lines=[
        f"👤 {name}",
        f"نقش: {ROLE_LABELS.get(target['role'],target['role'])}",
        f"وضعیت: {'فعال ✅' if target['is_active'] else 'غیرفعال ⛔️'}",
        "",
        "دسترسی‌ها:",
    ]
    for field,label in TEAM_PERMISSIONS.items():
        lines.append(f"{'✅' if target.get(field) else '❌'} {label}")
    buttons=[]
    if can_manage_target(actor,target):
        buttons.append([
            B(
                "⛔️ غیرفعال‌سازی" if target["is_active"] else "✅ فعال‌سازی",
                callback_data=f"team_toggle_{member_id}",
            )
        ])
        role_buttons=[
            B("مشاور",callback_data=f"team_setrole_{member_id}_ADVISOR"),
            B("منشی",callback_data=f"team_setrole_{member_id}_SECRETARY"),
        ]
        if actor["role"]=="AGENCY_OWNER":
            role_buttons.append(
                B("مدیر",callback_data=f"team_setrole_{member_id}_AGENCY_ADMIN")
            )
        buttons.append(role_buttons)
        for field,label in TEAM_PERMISSIONS.items():
            if field=="can_manage_team" and actor["role"]!="AGENCY_OWNER":
                continue
            buttons.append([
                B(
                    f"{'خاموش' if target.get(field) else 'روشن'}: {label}",
                    callback_data=f"team_perm_{member_id}_{field}",
                )
            ])
    buttons.append([B("⬅️ اعضا",callback_data="team_members")])
    await q.edit_message_text(
        "\n".join(lines),
        reply_markup=M(buttons),
    )


async def agency_stats(q,mem):
    r=db.one("""SELECT count(*) total,count(*) FILTER(WHERE status='ACTIVE') active,count(*) FILTER(WHERE created_at>=date_trunc('month',NOW())) month FROM partner_submissions WHERE agency_id=%s""",(mem['agency_id'],))
    pubs=db.one("SELECT count(*) n FROM partner_publication_jobs j JOIN partner_submissions s ON s.id=j.submission_id WHERE s.agency_id=%s AND j.status='PUBLISHED'",(mem['agency_id'],))
    await q.edit_message_text(f"📊 آمار بنگاه\n\nکل فایل‌ها: {r['total']}\nفایل فعال: {r['active']}\nثبت این ماه: {r['month']}\nانتشار موفق: {pubs['n']}",reply_markup=M([[B("⬅️",callback_data="home")]]))


async def on_error(update,context):
    log.error("partner update failed",exc_info=context.error)
    try:
        message=getattr(update,"effective_message",None)
        if message: await message.reply_text("⚠️ مشکلی پیش اومد؛ لطفاً دوباره از /menu امتحان کنید.")
    except Exception: pass


def add_handlers(app):
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start",start)); app.add_handler(CommandHandler("menu",menu))
    app.add_handler(CallbackQueryHandler(on_callback)); app.add_handler(MessageHandler(filters.PHOTO,handle_photo)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))


def availability_keyboard(submission_id):
    return M([
        [
            B("✅ موجود است",callback_data=f"avail_yes_{submission_id}"),
            B("💰 تغییر قیمت",callback_data=f"avail_price_{submission_id}"),
        ],
        [B("❌ فروخته / حذف شد",callback_data=f"avail_sold_{submission_id}")],
    ])


def availability_authorized(mem,submission):
    return bool(
        mem
        and mem["agency_id"]==submission["agency_id"]
        and (
            mem["role"] in ("AGENCY_OWNER","AGENCY_ADMIN")
            or mem["id"]==submission["submitted_by_member_id"]
        )
    )


async def agency_delivery_bot(hub_app,agency_id):
    connection=db.one(
        """SELECT encrypted_token
           FROM partner_bot_connections
           WHERE agency_id=%s AND status='ACTIVE'""",
        (agency_id,),
    )
    if connection:
        return Bot(decrypt_token(connection["encrypted_token"]))
    return hub_app.bot


async def send_availability_prompt(hub_app,submission,reminder=False):
    primary=await agency_delivery_bot(hub_app,submission["agency_id"])
    prefix="🔔 یادآوری تأیید فایل" if reminder else "🏠 تأیید موجودی فایل"
    text=(
        f"{prefix}\n\n"
        f"کد: {submission['tracking_code']}\n"
        f"{submission['district']} | {submission['area_m2']} متر\n\n"
        "این فایل هنوز موجود است؟"
    )
    kwargs=dict(
        chat_id=submission["telegram_user_id"],
        text=text,
        reply_markup=availability_keyboard(submission["id"]),
    )
    try:
        await primary.send_message(**kwargs)
    except TelegramError:
        if primary is hub_app.bot:
            raise
        log.info(
            "tenant availability delivery unavailable; using hub bot submission=%s",
            submission["id"],
        )
        await hub_app.bot.send_message(**kwargs)


async def refresh_agency_channel_post(submission_id,note=None):
    row=db.one(
        """SELECT s.*,j.telegram_message_id,c.telegram_channel_id,
                  b.encrypted_token
           FROM partner_submissions s
           JOIN partner_publication_jobs j ON j.submission_id=s.id
           JOIN partner_channel_connections c ON c.id=j.channel_connection_id
           JOIN partner_bot_connections b ON b.agency_id=s.agency_id
           WHERE s.id=%s
             AND j.target_type='AGENCY_CHANNEL'
             AND j.status='PUBLISHED'
             AND j.telegram_message_id IS NOT NULL
             AND b.status='ACTIVE'
           ORDER BY j.published_at DESC,j.id DESC
           LIMIT 1""",
        (submission_id,),
    )
    if not row:
        return
    caption=ct.fit_caption(row,db.brand(row["agency_id"]))
    if note:
        caption=(caption+f"\n\n{note}")[:ct.MEDIA_CAPTION_LIMIT]
    media=db.one(
        "SELECT id FROM partner_submission_media WHERE submission_id=%s LIMIT 1",
        (submission_id,),
    )
    bot=Bot(decrypt_token(row["encrypted_token"]))
    try:
        if media:
            await bot.edit_message_caption(
                chat_id=row["telegram_channel_id"],
                message_id=row["telegram_message_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.edit_message_text(
                chat_id=row["telegram_channel_id"],
                message_id=row["telegram_message_id"],
                text=caption,
                parse_mode=ParseMode.HTML,
            )
    except TelegramError:
        log.exception(
            "partner channel refresh failed submission=%s",
            submission_id,
        )


def confirm_submission_availability(submission_id):
    db.execute(
        """UPDATE partner_submissions
           SET status='ACTIVE',
               last_availability_confirmed_at=NOW(),
               availability_check_due_at=NOW()+interval '3 days',
               availability_check_sent_at=NULL,
               availability_reminder_sent_at=NULL,
               availability_missed_cycles=0,
               availability_paused_at=NULL,
               status_before_availability_pause=NULL,
               availability_last_error=NULL,
               updated_at=NOW()
           WHERE id=%s AND status IN('ACTIVE','INACTIVE')""",
        (submission_id,),
    )
    row=db.one(
        "SELECT central_property_id FROM partner_submissions WHERE id=%s",
        (submission_id,),
    )
    if row and row.get("central_property_id"):
        if db.one(
            "UPDATE properties SET status='ACTIVE',updated_at=NOW() WHERE id=%s AND status='INACTIVE' RETURNING id",
            (row["central_property_id"],),
        ):
            db.central_event(row["central_property_id"],"STATUS")


async def availability_callback(q,context,a,mem):
    match=re.fullmatch(r"avail_(yes|price|sold)_(\d+)",a)
    if not match:
        await q.answer("درخواست نامعتبر است.",show_alert=True); return
    action,submission_id=match.group(1),int(match.group(2))
    submission=db.one(
        "SELECT * FROM partner_submissions WHERE id=%s",
        (submission_id,),
    )
    if not submission or not availability_authorized(mem,submission):
        await q.answer("اجازه مدیریت این فایل را ندارید.",show_alert=True); return

    if action=="yes":
        if submission["status"]=="SOLD":
            await q.answer("فایل فروخته‌شده قابل فعال‌سازی نیست.",show_alert=True); return
        confirm_submission_availability(submission_id)
        db.audit(
            "SUBMISSION_AVAILABILITY_CONFIRMED",
            q.from_user.id,
            mem["agency_id"],
            "SUBMISSION",
            submission_id,
        )
        await refresh_agency_channel_post(submission_id)
        await q.edit_message_text(
            "✅ موجودی فایل تأیید شد. سه روز دیگر دوباره بررسی می‌شود."
        ); return

    if action=="price":
        context.user_data.clear()
        context.user_data.update(
            flow="availability_price",
            availability_submission_id=submission_id,
        )
        await q.edit_message_text(
            "قیمت جدید کل ملک را ارسال کنید.\nمثال: ۷.۸ میلیارد"
        ); return

    db.execute(
        """UPDATE partner_submissions
           SET status='SOLD',
               availability_check_due_at=NULL,
               availability_check_sent_at=NULL,
               availability_reminder_sent_at=NULL,
               availability_last_error=NULL,
               updated_at=NOW()
           WHERE id=%s""",
        (submission_id,),
    )
    if submission.get("central_property_id"):
        db.execute(
            "UPDATE properties SET status='INACTIVE',updated_at=NOW() WHERE id=%s",
            (submission["central_property_id"],),
        )
        db.central_event(submission["central_property_id"],"SOLD")
    await refresh_agency_channel_post(
        submission_id,
        "⛔️ این فایل دیگر موجود نیست.",
    )
    db.audit(
        "SUBMISSION_MARKED_SOLD",
        q.from_user.id,
        mem["agency_id"],
        "SUBMISSION",
        submission_id,
    )
    await q.edit_message_text("✅ فایل از وضعیت موجود خارج شد.")


async def availability_price_text(update,context):
    mem=db.member(update.effective_user.id)
    submission_id=context.user_data.get("availability_submission_id")
    submission=db.one(
        "SELECT * FROM partner_submissions WHERE id=%s",
        (submission_id,),
    )
    if not submission or not availability_authorized(mem,submission):
        context.user_data.clear()
        await update.message.reply_text("اجازه تغییر این فایل را ندارید."); return
    try:
        new_price=price(update.message.text)
    except Exception:
        await update.message.reply_text(
            "قیمت قابل تشخیص نیست. مثال: ۷.۸ میلیارد"
        ); return

    db.execute(
        """UPDATE partner_submissions
           SET current_price_toman=%s,updated_at=NOW()
           WHERE id=%s""",
        (new_price,submission_id),
    )
    if submission.get("central_property_id"):
        pcols=db.table_columns("properties")
        assignments=[]
        params=[]
        if "current_price_toman" in pcols:
            assignments.append("current_price_toman=%s")
            params.append(new_price)
        if "current_price_billion" in pcols:
            assignments.append("current_price_billion=%s")
            params.append(new_price/1_000_000_000)
        if "price_hashtag" in pcols:
            assignments.append("price_hashtag=%s")
            params.append(db.price_hashtag(new_price))
        if assignments:
            if "updated_at" in pcols:
                assignments.append("updated_at=NOW()")
            params.append(submission["central_property_id"])
            db.execute(
                f"UPDATE properties SET {','.join(assignments)} WHERE id=%s",
                params,
            )
            db.central_event(submission["central_property_id"],"PRICE",submission.get("current_price_toman"))

    confirm_submission_availability(submission_id)
    await refresh_agency_channel_post(submission_id)
    db.audit(
        "SUBMISSION_PRICE_AND_AVAILABILITY_UPDATED",
        update.effective_user.id,
        mem["agency_id"],
        "SUBMISSION",
        submission_id,
        {"current_price_toman":new_price},
    )
    context.user_data.clear()
    await update.message.reply_text(
        "✅ قیمت به‌روزرسانی و موجودی فایل برای سه روز تأیید شد.",
        reply_markup=home_keyboard(mem),
    )


async def pause_unconfirmed_submission(hub_app,submission):
    db.execute(
        """UPDATE partner_submissions
           SET status_before_availability_pause=status,
               status='INACTIVE',
               availability_paused_at=NOW(),
               availability_check_due_at=NULL,
               availability_check_sent_at=NULL,
               availability_reminder_sent_at=NULL,
               updated_at=NOW()
           WHERE id=%s AND status='ACTIVE'""",
        (submission["id"],),
    )
    if submission.get("central_property_id"):
        if db.one(
            "UPDATE properties SET status='INACTIVE',updated_at=NOW() WHERE id=%s AND status='ACTIVE' RETURNING id",
            (submission["central_property_id"],),
        ):
            db.central_event(submission["central_property_id"],"STATUS")
    await refresh_agency_channel_post(
        submission["id"],
        "⏸ اعتبار فایل در انتظار تأیید مشاور است.",
    )
    bot=await agency_delivery_bot(hub_app,submission["agency_id"])
    try:
        await bot.send_message(
            submission["telegram_user_id"],
            "⏸ چون دو نوبت به تأیید موجودی پاسخ داده نشد، "
            f"فایل {submission['tracking_code']} موقتاً غیرفعال شد.\n"
            "اگر هنوز موجود است، آن را دوباره فعال کنید.",
            reply_markup=M([[
                B(
                    "✅ فعال‌سازی مجدد",
                    callback_data=f"avail_yes_{submission['id']}",
                )
            ]]),
        )
    except TelegramError as exc:
        log.warning(
            "availability pause notice failed submission=%s error=%s",
            submission["id"],
            exc,
        )
    db.audit(
        "SUBMISSION_AUTO_PAUSED",
        None,
        submission["agency_id"],
        "SUBMISSION",
        submission["id"],
        {"reason":"two_unanswered_cycles"},
    )


async def run_availability_cycle(hub_app):
    expired=db.all_rows(
        """SELECT s.*,m.telegram_user_id
           FROM partner_submissions s
           JOIN partner_members m ON m.id=s.submitted_by_member_id
           WHERE s.status='ACTIVE'
             AND s.availability_check_sent_at IS NOT NULL
             AND s.availability_check_sent_at<=NOW()-interval '3 days'
           ORDER BY s.availability_check_sent_at
           LIMIT 100"""
    )
    for row in expired:
        if row["availability_missed_cycles"]>=1:
            await pause_unconfirmed_submission(hub_app,row)
        else:
            db.execute(
                """UPDATE partner_submissions
                   SET availability_missed_cycles=1,
                       availability_check_due_at=NOW(),
                       availability_check_sent_at=NULL,
                       availability_reminder_sent_at=NULL,
                       updated_at=NOW()
                   WHERE id=%s""",
                (row["id"],),
            )

    reminders=db.all_rows(
        """SELECT s.*,m.telegram_user_id
           FROM partner_submissions s
           JOIN partner_members m ON m.id=s.submitted_by_member_id
           WHERE s.status='ACTIVE'
             AND s.availability_check_sent_at IS NOT NULL
             AND s.availability_reminder_sent_at IS NULL
             AND s.availability_check_sent_at<=NOW()-interval '1 day'
           ORDER BY s.availability_check_sent_at
           LIMIT 100"""
    )
    for row in reminders:
        error=None
        try:
            await send_availability_prompt(hub_app,row,reminder=True)
        except Exception as exc:
            error=str(exc)[:1000]
            log.warning(
                "availability reminder failed submission=%s error=%s",
                row["id"],
                exc,
            )
        if error:
            db.execute(
                """UPDATE partner_submissions
                   SET availability_last_error=%s,updated_at=NOW()
                   WHERE id=%s""",
                (error,row["id"]),
            )
        else:
            db.execute(
                """UPDATE partner_submissions
                   SET availability_reminder_sent_at=NOW(),
                       availability_last_error=NULL,
                       updated_at=NOW()
                   WHERE id=%s""",
                (row["id"],),
            )

    due=db.all_rows(
        """SELECT s.*,m.telegram_user_id
           FROM partner_submissions s
           JOIN partner_members m ON m.id=s.submitted_by_member_id
           WHERE s.status='ACTIVE'
             AND s.availability_check_due_at<=NOW()
             AND s.availability_check_sent_at IS NULL
           ORDER BY s.availability_check_due_at
           LIMIT 100"""
    )
    for row in due:
        error=None
        try:
            await send_availability_prompt(hub_app,row)
        except Exception as exc:
            error=str(exc)[:1000]
            log.warning(
                "availability prompt failed submission=%s error=%s",
                row["id"],
                exc,
            )
        if error:
            db.execute(
                """UPDATE partner_submissions
                   SET availability_check_due_at=NOW()+interval '1 hour',
                       availability_last_error=%s,
                       updated_at=NOW()
                   WHERE id=%s""",
                (error,row["id"]),
            )
        else:
            db.execute(
                """UPDATE partner_submissions
                   SET availability_check_sent_at=NOW(),
                       availability_reminder_sent_at=NULL,
                       availability_last_error=NULL,
                       updated_at=NOW()
                   WHERE id=%s""",
                (row["id"],),
            )


async def availability_worker(hub_app):
    log.info("Partner three-day availability worker started.")
    while True:
        try:
            await run_availability_cycle(hub_app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("partner availability worker cycle")
        await asyncio.sleep(300)


async def tenant_supervisor(hub_app:Application):
    """Hot-load each agency bot as a white-label Telegram front end."""
    running={}
    while True:
        try:
            rows=db.all_rows("SELECT agency_id,bot_id,encrypted_token FROM partner_bot_connections WHERE status='ACTIVE'")
            wanted={r['agency_id']:r for r in rows}
            for aid,row in wanted.items():
                if aid in running: continue
                try:
                    app=Application.builder().token(decrypt_token(row['encrypted_token'])).build(); add_handlers(app)
                    await app.initialize()
                    await configure_blue_menu(app)
                    await app.start(); await app.updater.start_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=False)
                    running[aid]=app; log.info("white-label bot started agency=%s",aid)
                except Exception as exc:
                    log.exception("tenant bot failed agency=%s",aid); db.execute("UPDATE partner_bot_connections SET status='ERROR',last_error=%s,updated_at=NOW() WHERE agency_id=%s",(str(exc)[:2000],aid))
            for aid in list(running):
                if aid not in wanted:
                    app=running.pop(aid); await app.updater.stop(); await app.stop(); await app.shutdown()
        except Exception: log.exception("tenant supervisor cycle")
        await asyncio.sleep(30)


def central_queued(submission_id):
    """Queue the file for the central channel; True when it is on its way."""
    return central.queue(submission_id) or db.one(
        """SELECT 1 FROM partner_publication_jobs WHERE submission_id=%s AND target_type='CENTRAL_CHANNEL'
           AND status IN ('PENDING','PROCESSING','PUBLISHED')""",(submission_id,)) is not None


async def publish_scheduled_job(job,hub_bot=None):
    s=db.one("SELECT * FROM partner_submissions WHERE id=%s",(job['submission_id'],))
    ch=db.one("SELECT * FROM partner_channel_connections WHERE id=%s AND is_active",(job['channel_connection_id'],))
    if not ch: raise RuntimeError("active bot/channel connection not found")
    return await ct.publish_submission(s,ch,[hub_bot] if hub_bot else [])


async def schedule_worker(app:Application):
    last_auto_queue=None
    while True:
        try:
            if last_auto_queue is None or datetime.now(TEHRAN)-last_auto_queue>=timedelta(minutes=5):
                last_auto_queue=datetime.now(TEHRAN)
                queued=ct.auto_queue_all()
                if queued: log.info("agency channel auto-queue: files=%s",queued)
                backlog=central.queue_backlog()
                if backlog: log.info("central channel backlog queued: files=%s",backlog)
        except Exception: log.exception("agency channel auto-queue")
        try:
            await central.run_due(OWNER_ID,[app.bot],log)
        except Exception: log.exception("central channel queue")
        try:
            # A paused agency schedule holds its queue.
            jobs=db.all_rows("""SELECT j.*,s.status AS submission_status FROM partner_publication_jobs j
                                JOIN partner_submissions s ON s.id=j.submission_id
                                LEFT JOIN partner_publish_schedules ps ON ps.agency_id=s.agency_id
                                WHERE j.target_type='AGENCY_CHANNEL' AND j.status='SCHEDULED' AND j.scheduled_for<=NOW()
                                  AND COALESCE(ps.is_active,TRUE)
                                ORDER BY j.scheduled_for,j.id LIMIT 10""")
            for job in jobs:
                if job['submission_status'] not in ct.PUBLISHABLE_STATUSES:
                    db.execute("UPDATE partner_publication_jobs SET status='CANCELLED',updated_at=NOW() WHERE id=%s AND status='SCHEDULED'",(job['id'],)); continue
                locked=db.one("UPDATE partner_publication_jobs SET status='PROCESSING',attempts=attempts+1,updated_at=NOW() WHERE id=%s AND status='SCHEDULED' RETURNING id",(job['id'],))
                if not locked: continue
                try:
                    ids=await publish_scheduled_job(job,app.bot)
                    ct.record_publication(job['submission_id'],job['channel_connection_id'],ids,job_id=job['id'])
                    log.info("agency channel post published: job=%s submission=%s messages=%s",job['id'],job['submission_id'],ids)
                except Exception as exc:
                    log.warning("agency channel post failed: job=%s submission=%s attempt=%s error=%s",job['id'],job['submission_id'],job['attempts']+1,exc)
                    db.execute("UPDATE partner_publication_jobs SET status=CASE WHEN attempts>=5 THEN 'FAILED' ELSE 'SCHEDULED' END,last_error=%s,scheduled_for=NOW()+interval '5 minutes',updated_at=NOW() WHERE id=%s",(str(exc)[:2000],job['id']))
        except Exception: log.exception("schedule worker cycle")
        await asyncio.sleep(20)


async def post_init(app):
    db.ensure_partner_v2_schema()
    db.ensure_partner_v3_schema()
    db.ensure_central_schema()
    await configure_blue_menu(app)
    asyncio.create_task(tenant_supervisor(app),name="tenant-supervisor")
    asyncio.create_task(schedule_worker(app),name="partner-schedule-worker")
    asyncio.create_task(
        availability_worker(app),
        name="partner-availability-worker",
    )


def main():
    app=Application.builder().token(HUB_TOKEN).post_init(post_init).build(); add_handlers(app)
    log.info("RealEstate Partner Hub starting"); app.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=False)


if __name__=="__main__": main()
