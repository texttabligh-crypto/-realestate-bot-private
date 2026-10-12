from __future__ import annotations
import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot, BotCommand, InlineKeyboardButton as B, InlineKeyboardMarkup as M, InputMediaPhoto, MenuButtonCommands, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import db
from formatter import public_caption
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
    if owner: rows += [[B("🏢 درخواست‌های عضویت",callback_data="o_pending"),B("🧾 صف بررسی فایل",callback_data="o_submissions")],[B("📊 آمار شبکه",callback_data="o_stats")]]
    if member and member["agency_status"] == "ACTIVE":
        rows += [[B("➕ ثبت فایل جدید",callback_data="p_add"),B("🏠 فایل‌های من",callback_data="p_files")],
                 [B("🎨 برند و وایت‌لیبل",callback_data="p_brand"),B("🔌 اتصال ربات و کانال",callback_data="p_connect")],
                 [B("👥 تیم و دعوت",callback_data="p_team"),B("📊 آمار بنگاه",callback_data="p_stats")]]
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
    if a=="p_add": await begin_property(q,context); return
    if a.startswith("pf_") or a.startswith("ptype_") or a.startswith("deal_"):
        await property_callback(q,context,a,mem); return
    if a=="p_files": await list_files(q,mem); return
    if a=="p_brand": await brand_menu(q,mem); return
    if a.startswith("brand_"): await brand_callback(q,context,a,mem); return
    if a=="p_connect": await connection_menu(q,mem); return
    if a.startswith("conn_"): await connection_callback(q,context,a,mem); return
    if a=="p_team": await team_menu(q,mem); return
    if a=="team_invite": await create_invite(q,mem); return
    if a=="p_stats": await agency_stats(q,mem); return


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
    code=update.message.text.strip().upper(); row=db.one("""SELECT i.*,a.status agency_status FROM partner_invites i JOIN partner_agencies a ON a.id=i.agency_id
        WHERE i.code=%s AND i.used_count<i.max_uses AND (i.expires_at IS NULL OR i.expires_at>NOW())""",(code,))
    if not row or row['agency_status']!='ACTIVE': await update.message.reply_text("کد دعوت معتبر یا فعال نیست."); return
    u=update.effective_user
    with db.connect() as c,c.cursor() as cur:
        cur.execute("INSERT INTO partner_members(agency_id,telegram_user_id,username,display_name,role) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",(row['agency_id'],u.id,u.username,u.full_name,row['role']))
        cur.execute("UPDATE partner_invites SET used_count=used_count+1 WHERE id=%s",(row['id'],)); c.commit()
    context.user_data.clear(); db.audit("MEMBER_JOINED",u.id,row['agency_id'],"MEMBER",None)
    await update.message.reply_text("✅ به تیم بنگاه اضافه شدید.",reply_markup=home_keyboard(db.member(u.id)))


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
        sid=int(a.rsplit('_',1)[1]); pid=db.push_to_central(sid,OWNER_ID)
        await q.edit_message_text(f"✅ تأیید شد و فایل مرکزی #{pid} با اولویت Partner وارد صف شد.",reply_markup=M([[B("⬅️ صف",callback_data="o_submissions")]])); return
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


async def property_callback(q,context,a,mem):
    st=context.user_data
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
    if a=="pf_submit": await save_submission(q,context,mem); return
    if a=="pf_own_now": await publish_own(q,context,mem); return
    if a=="pf_own_schedule":
        context.user_data['flow']='schedule'
        await q.edit_message_text("زمان انتشار را بفرستید.\nمثال: فردا ۱۰:۳۰\nیا: ۱۴۰۵/۰۷/۱۸ ۱۸:۰۰")
        return
    if a=="pf_central":
        sid=st.get('saved_submission_id'); db.execute("UPDATE partner_submissions SET status='SUBMITTED',updated_at=NOW() WHERE id=%s AND agency_id=%s",(sid,mem['agency_id']))
        db.execute("INSERT INTO partner_publication_jobs(submission_id,target_type,status) VALUES(%s,'CENTRAL_CHANNEL','PENDING')",(sid,)); db.audit("CENTRAL_REVIEW_REQUESTED",q.from_user.id,mem['agency_id'],"SUBMISSION",sid)
        await q.edit_message_text("✅ فایل برای بررسی و انتشار در کانال مرکزی ارسال شد.",reply_markup=M([[B("⬅️ منو",callback_data="home")]])); return
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
    if own and mem['can_publish_own']:
        rows.append([B("🚀 انتشار فوری",callback_data="pf_own_now"),B("🗓 زمان‌بندی",callback_data="pf_own_schedule")])
    rows += [[B("🌐 ارسال برای کانال مرکزی",callback_data="pf_central")],[B("✅ فعلاً تمام",callback_data="pf_done")]]
    warning=f"\n\n⚠️ مشابه فایل {duplicate['tracking_code']} تشخیص داده شد؛ مدیر مرکزی هنگام بررسی آن را می‌بیند." if duplicate else ""
    await q.edit_message_text(f"✅ فایل ثبت شد.\nکد پیگیری: {code}{warning}\n\nمقصد انتشار را انتخاب کنید:",reply_markup=M(rows))


async def publish_own(q,context,mem):
    sid=context.user_data['saved_submission_id']; s=db.one("SELECT * FROM partner_submissions WHERE id=%s AND agency_id=%s",(sid,mem['agency_id']))
    conn=db.one("SELECT * FROM partner_channel_connections WHERE agency_id=%s AND is_active AND is_default LIMIT 1",(mem['agency_id'],)); bc=db.one("SELECT * FROM partner_bot_connections WHERE agency_id=%s AND status='ACTIVE'",(mem['agency_id'],))
    if not conn or not bc: await q.answer("ابتدا ربات و کانال بنگاه را متصل کنید.",show_alert=True); return
    bot=Bot(decrypt_token(bc['encrypted_token'])); cap=public_caption(s,db.brand(mem['agency_id']))
    media=db.all_rows("SELECT * FROM partner_submission_media WHERE submission_id=%s ORDER BY position",(sid,))
    try:
        if media:
            group=[InputMediaPhoto(m['telegram_file_id'],caption=cap if i==0 else None,parse_mode=ParseMode.HTML) for i,m in enumerate(media[:10])]
            sent=await bot.send_media_group(conn['telegram_channel_id'],group); mid=sent[0].message_id
        else: mid=(await bot.send_message(conn['telegram_channel_id'],cap,parse_mode=ParseMode.HTML)).message_id
        db.execute("INSERT INTO partner_publication_jobs(submission_id,target_type,channel_connection_id,status,published_at,telegram_message_id) VALUES(%s,'AGENCY_CHANNEL',%s,'PUBLISHED',NOW(),%s)",(sid,conn['id'],mid))
        db.execute("UPDATE partner_submissions SET status='ACTIVE',updated_at=NOW() WHERE id=%s",(sid,))
        db.audit("AGENCY_CHANNEL_PUBLISHED",q.from_user.id,mem['agency_id'],"SUBMISSION",sid,{"message_id":mid})
        await q.edit_message_text("✅ با برند بنگاه در کانال خودتان منتشر شد.",reply_markup=M([[B("🌐 ارسال برای کانال مرکزی",callback_data="pf_central")],[B("⬅️ منو",callback_data="home")]]))
    except Exception as exc:
        db.audit("AGENCY_PUBLISH_FAILED",q.from_user.id,mem['agency_id'],"SUBMISSION",sid,{"error":str(exc)})
        await q.edit_message_text("❌ انتشار انجام نشد. دسترسی ادمین ربات به کانال و شناسه کانال را بررسی کنید.\n"+str(exc)[:500],reply_markup=M([[B("⬅️ اتصال‌ها",callback_data="p_connect")]]))


async def schedule_text(update,context):
    mem=db.member(update.effective_user.id); raw=digits(update.message.text.strip()); now=datetime.now(TEHRAN)
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
        db.execute("INSERT INTO partner_publication_jobs(submission_id,target_type,channel_connection_id,status,scheduled_for) VALUES(%s,'AGENCY_CHANNEL',%s,'SCHEDULED',%s)",(sid,ch['id'],local))
        db.audit("AGENCY_PUBLICATION_SCHEDULED",update.effective_user.id,mem['agency_id'],"SUBMISSION",sid,{"scheduled_for":local.isoformat()})
        context.user_data.clear(); await update.message.reply_text(f"✅ انتشار برای {local.strftime('%Y-%m-%d %H:%M')} زمان‌بندی شد.",reply_markup=home_keyboard(mem))
    except Exception:
        await update.message.reply_text("زمان قابل تشخیص نیست یا در گذشته است.\nمثال دقیق: فردا ۱۰:۳۰")


async def list_files(q,mem):
    rows=db.all_rows("SELECT tracking_code,district,area_m2,status FROM partner_submissions WHERE agency_id=%s ORDER BY id DESC LIMIT 20",(mem['agency_id'],))
    text="🏠 آخرین فایل‌های بنگاه\n\n"+("\n".join(f"• {r['tracking_code']} | {r['district']} | {r['area_m2']}متر | {r['status']}" for r in rows) or "فایلی ثبت نشده.")
    await q.edit_message_text(text,reply_markup=M([[B("⬅️",callback_data="home")]]))


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


async def team_menu(q,mem):
    rows=db.all_rows("SELECT display_name,username,role FROM partner_members WHERE agency_id=%s AND is_active ORDER BY id",(mem['agency_id'],))
    text="👥 تیم بنگاه\n\n"+"\n".join(f"• {r['display_name'] or r['username']} — {r['role']}" for r in rows)
    await q.edit_message_text(text,reply_markup=M([[B("➕ ساخت کد دعوت",callback_data="team_invite")],[B("⬅️",callback_data="home")]]))


async def create_invite(q,mem):
    if mem['role'] not in ('AGENCY_OWNER','AGENCY_ADMIN'): await q.answer("دسترسی ندارید.",show_alert=True); return
    code=hashlib.sha256(os.urandom(32)).hexdigest()[:10].upper()
    db.execute("INSERT INTO partner_invites(agency_id,code,expires_at,created_by_member_id) VALUES(%s,%s,NOW()+interval '7 days',%s)",(mem['agency_id'],code,mem['id']))
    await q.edit_message_text(f"🔑 کد دعوت یک‌بارمصرف:\n<code>{code}</code>\n\nاعتبار: ۷ روز",parse_mode=ParseMode.HTML,reply_markup=M([[B("⬅️ تیم",callback_data="p_team")]]))


async def agency_stats(q,mem):
    r=db.one("""SELECT count(*) total,count(*) FILTER(WHERE status='ACTIVE') active,count(*) FILTER(WHERE created_at>=date_trunc('month',NOW())) month FROM partner_submissions WHERE agency_id=%s""",(mem['agency_id'],))
    pubs=db.one("SELECT count(*) n FROM partner_publication_jobs j JOIN partner_submissions s ON s.id=j.submission_id WHERE s.agency_id=%s AND j.status='PUBLISHED'",(mem['agency_id'],))
    await q.edit_message_text(f"📊 آمار بنگاه\n\nکل فایل‌ها: {r['total']}\nفایل فعال: {r['active']}\nثبت این ماه: {r['month']}\nانتشار موفق: {pubs['n']}",reply_markup=M([[B("⬅️",callback_data="home")]]))


def add_handlers(app):
    app.add_handler(CommandHandler("start",start)); app.add_handler(CommandHandler("menu",menu))
    app.add_handler(CallbackQueryHandler(on_callback)); app.add_handler(MessageHandler(filters.PHOTO,handle_photo)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))


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


async def publish_scheduled_job(job):
    s=db.one("SELECT * FROM partner_submissions WHERE id=%s",(job['submission_id'],)); aid=s['agency_id']
    bc=db.one("SELECT * FROM partner_bot_connections WHERE agency_id=%s AND status='ACTIVE'",(aid,)); ch=db.one("SELECT * FROM partner_channel_connections WHERE id=%s AND is_active",(job['channel_connection_id'],))
    if not bc or not ch: raise RuntimeError("active bot/channel connection not found")
    bot=Bot(decrypt_token(bc['encrypted_token'])); cap=public_caption(s,db.brand(aid)); media=db.all_rows("SELECT * FROM partner_submission_media WHERE submission_id=%s ORDER BY position",(s['id'],))
    if media:
        sent=await bot.send_media_group(ch['telegram_channel_id'],[InputMediaPhoto(m['telegram_file_id'],caption=cap if i==0 else None,parse_mode=ParseMode.HTML) for i,m in enumerate(media[:10])]); return sent[0].message_id
    return (await bot.send_message(ch['telegram_channel_id'],cap,parse_mode=ParseMode.HTML)).message_id


async def schedule_worker(app:Application):
    while True:
        try:
            jobs=db.all_rows("""SELECT * FROM partner_publication_jobs WHERE target_type='AGENCY_CHANNEL' AND status='SCHEDULED' AND scheduled_for<=NOW() ORDER BY scheduled_for,id LIMIT 10""")
            for job in jobs:
                locked=db.one("UPDATE partner_publication_jobs SET status='PROCESSING',attempts=attempts+1,updated_at=NOW() WHERE id=%s AND status='SCHEDULED' RETURNING id",(job['id'],))
                if not locked: continue
                try:
                    mid=await publish_scheduled_job(job); db.execute("UPDATE partner_publication_jobs SET status='PUBLISHED',published_at=NOW(),telegram_message_id=%s,updated_at=NOW() WHERE id=%s",(mid,job['id'])); db.execute("UPDATE partner_submissions SET status='ACTIVE',updated_at=NOW() WHERE id=%s",(job['submission_id'],))
                except Exception as exc:
                    db.execute("UPDATE partner_publication_jobs SET status=CASE WHEN attempts>=5 THEN 'FAILED' ELSE 'SCHEDULED' END,last_error=%s,scheduled_for=NOW()+interval '5 minutes',updated_at=NOW() WHERE id=%s",(str(exc)[:2000],job['id']))
        except Exception: log.exception("schedule worker cycle")
        await asyncio.sleep(20)


async def post_init(app):
    await configure_blue_menu(app)
    asyncio.create_task(tenant_supervisor(app),name="tenant-supervisor")
    asyncio.create_task(schedule_worker(app),name="partner-schedule-worker")


def main():
    app=Application.builder().token(HUB_TOKEN).post_init(post_init).build(); add_handlers(app)
    log.info("RealEstate Partner Hub starting"); app.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=False)


if __name__=="__main__": main()
