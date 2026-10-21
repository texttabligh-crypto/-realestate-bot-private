from __future__ import annotations
import asyncio, hashlib, io, json, logging, os, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import psycopg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

TEHRAN = ZoneInfo('Asia/Tehran')
DB_HOST=os.getenv('POSTGRES_HOST','postgres'); DB_NAME=os.getenv('POSTGRES_DB'); DB_USER=os.getenv('POSTGRES_USER'); DB_PASSWORD=os.getenv('POSTGRES_PASSWORD')
SKIP='__SKIP__'
FIELDS=[
 ('property_type','نوع ملک',False),('district','منطقه',False),('address_text','محله / خیابان / آدرس',False),
 ('area_m2','متراژ',False),('bedrooms','تعداد خواب',True),('floor_text','طبقه',True),('total_floors','تعداد کل طبقات',True),
 ('units_per_floor','تعداد واحد در طبقه',True),('build_year','سال ساخت',True),('direction','جهت ملک',True),
 ('parking_text','پارکینگ (دارد / ندارد / توضیح)',True),('elevator_text','آسانسور',True),('storage_text','انباری',True),
 ('deed_text','سند',True),('installations_text','نصبیات / امکانات',True),('current_price_toman','قیمت کل به تومان',False),
 ('special_notes','توضیحات ویژه / توضیح آزاد',True),
]
TYPE_MAP={'آپارتمان':'APARTMENT','ویلایی':'VILLA','زمین':'LAND','تجاری':'COMMERCIAL','اداری':'OFFICE'}

def db_connect(): return psycopg.connect(host=DB_HOST,dbname=DB_NAME,user=DB_USER,password=DB_PASSWORD)
def fa_num(v): return str(v).translate(str.maketrans('0123456789','۰۱۲۳۴۵۶۷۸۹'))
def norm_digits(v): return str(v or '').translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))
def clean_int(v):
    s=re.sub(r'[^0-9]','',norm_digits(v)); return int(s) if s else None

def audit_event(actor_user_id, action, *, entity_type=None, entity_id=None, severity='INFO', details=None):
    try:
        with db_connect() as c, c.cursor() as cur:
            cur.execute("INSERT INTO audit_events(actor_user_id,action,entity_type,entity_id,severity,details) VALUES(%s,%s,%s,%s,%s,%s::jsonb)",(actor_user_id,action,entity_type,entity_id,severity,json.dumps(details or {},ensure_ascii=False)))
            c.commit()
    except Exception: logging.exception('audit_event failed')

def audit_login(user_id, success, username=None, telegram_user_id=None):
    audit_event(user_id if success else None,'LOGIN_SUCCESS' if success else 'LOGIN_FAILED',entity_type='USER',entity_id=user_id, severity='INFO' if success else 'WARNING', details={'username':username,'telegram_user_id':telegram_user_id})

def _scope(session, alias, owner_col='created_by_user_id'):
    if session[3]=='OWNER' or _has_perm(session,'stats_team_view'): return '',[]
    return f' AND {alias}.{owner_col} = %s',[session[0]]
def _has_perm(session,key):
    if session and session[3]=='OWNER': return True
    if not session: return False
    with db_connect() as c, c.cursor() as cur:
        cur.execute('SELECT is_allowed FROM user_permissions WHERE user_id=%s AND permission_key=%s',(session[0],key)); r=cur.fetchone()
    return bool(r and r[0])

def stats_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton('📊 نمای کلی',callback_data='stats_overview'),InlineKeyboardButton('📍 مناطق',callback_data='stats_regions')],[InlineKeyboardButton('👥 مشتریان',callback_data='stats_customers'),InlineKeyboardButton('🏠 فایل‌ها',callback_data='stats_files')],[InlineKeyboardButton('📅 بازدیدها',callback_data='stats_visits'),InlineKeyboardButton('🧑‍💼 مشاورها',callback_data='stats_advisors')],[InlineKeyboardButton('📡 منابع',callback_data='stats_sources'),InlineKeyboardButton('🚨 هشدارها',callback_data='logs_alerts')],[InlineKeyboardButton('⬅️ بازگشت',callback_data='home')]])
def logs_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton('🕘 آخرین فعالیت‌ها',callback_data='logs_recent'),InlineKeyboardButton('👤 بر اساس کاربر',callback_data='logs_users')],[InlineKeyboardButton('🧩 نوع عملیات',callback_data='logs_types'),InlineKeyboardButton('🏠 بر اساس فایل',callback_data='logs_property_prompt')],[InlineKeyboardButton('👥 بر اساس مشتری',callback_data='logs_customer_prompt'),InlineKeyboardButton('📆 بازه زمانی',callback_data='logs_range')],[InlineKeyboardButton('🚨 هشدارها',callback_data='logs_alerts')],[InlineKeyboardButton('⬅️ بازگشت',callback_data='home')]])

def _periods():
    now=datetime.now(TEHRAN); day=now.replace(hour=0,minute=0,second=0,microsecond=0); week=day-timedelta(days=day.weekday()); month=day.replace(day=1); return now,day,week,month

def _overview(session):
    now,day,week,month=_periods(); ps,pp=_scope(session,'p','manual_created_by_user_id'); cs,cp=_scope(session,'c','owner_user_id'); vs,vp=_scope(session,'v','assigned_user_id')
    with db_connect() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FILTER(WHERE status='ACTIVE'),count(*) FILTER(WHERE created_at>=%s),count(*) FILTER(WHERE created_at>=%s),count(*) FILTER(WHERE created_at>=%s),count(*) FILTER(WHERE status IN('SOLD','INACTIVE','INVALID')) FROM properties p WHERE 1=1"+ps,[day,week,month]+pp); pr=cur.fetchone()
        cur.execute('SELECT count(*),count(*) FILTER(WHERE created_at>=%s),count(*) FILTER(WHERE created_at>=%s),count(*) FILTER(WHERE created_at>=%s) FROM customer_profiles c WHERE 1=1'+cs,[day,week,month]+cp); cr=cur.fetchone()
        cur.execute("SELECT count(*),count(*) FILTER(WHERE visit_at>=%s AND visit_at<%s),count(*) FILTER(WHERE visit_at>=%s AND visit_at<%s),count(*) FILTER(WHERE visit_at>=%s AND visit_at<%s),count(*) FILTER(WHERE status='CLOSED'),count(*) FILTER(WHERE status='CANCELLED') FROM visit_requests v WHERE 1=1"+vs,[day,day+timedelta(days=1),day+timedelta(days=1),day+timedelta(days=2),week,week+timedelta(days=7)]+vp); vr=cur.fetchone()
        cur.execute("SELECT count(*) FROM customer_profiles c WHERE COALESCE(last_interaction_at,created_at) < %s"+cs,[now-timedelta(days=3)]+cp); unf=cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM visit_requests v WHERE status IN('NEW','CONTACTED','SCHEDULED') AND updated_at < %s"+vs,[now-timedelta(days=2)]+vp); uv=cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM users WHERE role='ADVISOR' AND is_active=TRUE"); advisors=cur.fetchone()[0]
    return ('📊 نمای کلی\n\n'+f'🏠 فایل فعال: {pr[0]}\nامروز: {pr[1]} | هفته: {pr[2]} | ماه: {pr[3]}\nفروخته/غیرفعال: {pr[4]}\n\n👥 مشتری: {cr[0]}\nجدید امروز: {cr[1]} | هفته: {cr[2]} | ماه: {cr[3]}\n\n📅 درخواست بازدید: {vr[0]}\nامروز: {vr[1]} | فردا: {vr[2]} | این هفته: {vr[3]}\nانجام‌شده: {vr[4]} | لغوشده: {vr[5]}\n\n⚠️ مشتری بدون پیگیری: {unf}\n⚠️ بازدید بدون پیگیری: {uv}\n🧑‍💼 مشاور فعال: {advisors}')

def _regions(session):
    ps,pp=_scope(session,'p','manual_created_by_user_id')
    with db_connect() as c,c.cursor() as cur:
        cur.execute("SELECT COALESCE(NULLIF(BTRIM(district),''),'بدون منطقه'),count(*) FROM properties p WHERE status='ACTIVE'"+ps+" GROUP BY 1 ORDER BY 2 DESC,1 LIMIT 30",pp); rows=cur.fetchall()
    return '📍 آمار مناطق (فقط district ساختاریافته)\n\n'+('\n'.join(f'• {r}: {n}' for r,n in rows) if rows else 'داده‌ای نیست.')
def _customers(session):
    cs,cp=_scope(session,'c','owner_user_id')
    with db_connect() as c,c.cursor() as cur:
        cur.execute("SELECT COALESCE(u.display_name,u.username,'بدون مشاور'),count(*) FROM customer_profiles c LEFT JOIN users u ON u.id=c.owner_user_id WHERE 1=1"+cs+' GROUP BY 1 ORDER BY 2 DESC',cp); by=cur.fetchall()
        cur.execute("SELECT COALESCE(NULLIF(BTRIM(p.district),''),'بدون منطقه'),count(DISTINCT v.customer_id) FROM visit_requests v JOIN properties p ON p.id=v.property_id JOIN customer_profiles c ON c.id=v.customer_id WHERE 1=1"+cs+' GROUP BY 1 ORDER BY 2 DESC LIMIT 15',cp); reg=cur.fetchall()
    return '👥 آمار مشتریان\n\nبر اساس مشاور:\n'+('\n'.join(f'• {a}: {n}' for a,n in by) or '—')+'\n\nمناطق مورد تقاضا (از بازدیدهای متصل به فایل):\n'+('\n'.join(f'• {a}: {n}' for a,n in reg) or '—')
def _files(session):
    ps,pp=_scope(session,'p','manual_created_by_user_id')
    with db_connect() as c,c.cursor() as cur:
        cur.execute("SELECT COALESCE(u.display_name,u.username,'ورودی خودکار'),count(*) FROM properties p LEFT JOIN users u ON u.id=p.manual_created_by_user_id WHERE 1=1"+ps+' GROUP BY 1 ORDER BY 2 DESC',pp); rows=cur.fetchall()
    return '🏠 فایل‌ها بر اساس ثبت‌کننده\n\n'+('\n'.join(f'• {a}: {n}' for a,n in rows) or '—')
def _visits(session):
    vs,vp=_scope(session,'v','assigned_user_id')
    with db_connect() as c,c.cursor() as cur:
        cur.execute("SELECT COALESCE(u.display_name,u.username,'بدون مشاور'),count(*),count(*) FILTER(WHERE v.status='CLOSED'),count(*) FILTER(WHERE v.status='CANCELLED') FROM visit_requests v LEFT JOIN users u ON u.id=v.assigned_user_id WHERE 1=1"+vs+' GROUP BY 1 ORDER BY 2 DESC',vp); rows=cur.fetchall()
    return '📅 آمار بازدیدها\n\n'+('\n'.join(f'• {a}: کل {n} | انجام {d} | لغو {x}' for a,n,d,x in rows) or '—')
def _advisors(session):
    if session[3] != 'OWNER' and not _has_perm(session,'stats_team_view'):
        uid=session[0]; extra=' WHERE u.id=%s'; params=[uid]
    else: extra=" WHERE u.role IN ('ADVISOR','ADMIN','OWNER') AND u.is_active=TRUE"; params=[]
    with db_connect() as c,c.cursor() as cur:
        cur.execute("SELECT COALESCE(u.display_name,u.username), (SELECT count(*) FROM customer_profiles c WHERE c.owner_user_id=u.id), (SELECT count(*) FROM visit_requests v WHERE v.assigned_user_id=u.id), (SELECT count(*) FROM properties p WHERE p.manual_created_by_user_id=u.id), (SELECT count(*) FROM audit_events a WHERE a.actor_user_id=u.id AND a.created_at>NOW()-interval '7 days') FROM users u"+extra+' ORDER BY 2 DESC',params); rows=cur.fetchall()
    return '🧑‍💼 آمار مشاورها\n\n'+('\n'.join(f'• {a}: مشتری {c} | بازدید {v} | فایل دستی {p} | فعالیت ۷روز {x}' for a,c,v,p,x in rows) or '—')
def _sources(session):
    ps,pp=_scope(session,'p','manual_created_by_user_id')
    with db_connect() as c,c.cursor() as cur:
        cur.execute("SELECT COALESCE(ch.title,ch.username,ch.telegram_channel_id::text,'نامشخص'),count(DISTINCT ps.property_id) FROM property_sources ps LEFT JOIN channels ch ON ch.id=ps.source_channel_id JOIN properties p ON p.id=ps.property_id WHERE ps.is_active=TRUE"+ps+' GROUP BY 1 ORDER BY 2 DESC LIMIT 30',pp); rows=cur.fetchall()
    return '📡 آمار منابع\n\n'+('\n'.join(f'• {a}: {n} فایل' for a,n in rows) or '—')

def _chart_rows(kind,session):
    ps,pp=_scope(session,'p','manual_created_by_user_id')
    with db_connect() as c,c.cursor() as cur:
        if kind=='properties': q="SELECT COALESCE(NULLIF(BTRIM(p.district),''),'بدون منطقه'),count(*) FROM properties p WHERE p.status='ACTIVE'"+ps+' GROUP BY 1 ORDER BY 2 DESC LIMIT 15'; params=pp
        elif kind=='customers':
            vs,vp=_scope(session,'v','assigned_user_id'); q="SELECT COALESCE(NULLIF(BTRIM(p.district),''),'بدون منطقه'),count(DISTINCT v.customer_id) FROM visit_requests v JOIN properties p ON p.id=v.property_id WHERE 1=1"+vs+" GROUP BY 1 ORDER BY 2 DESC LIMIT 15"; params=vp
        else:
            vs,vp=_scope(session,'v','assigned_user_id'); q="SELECT COALESCE(NULLIF(BTRIM(p.district),''),'بدون منطقه'),count(*) FROM visit_requests v JOIN properties p ON p.id=v.property_id WHERE 1=1"+vs+" GROUP BY 1 ORDER BY 2 DESC LIMIT 15"; params=vp
        cur.execute(q,params); return cur.fetchall()
def make_chart(kind,session):
    rows=_chart_rows(kind,session)
    if not rows:return None
    import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
    labels=[r[0] for r in rows][::-1]; values=[r[1] for r in rows][::-1]
    fig=plt.figure(figsize=(9,6)); ax=fig.add_axes([.30,.12,.66,.80]); ax.barh(range(len(labels)),values); ax.set_yticks(range(len(labels))); ax.set_yticklabels(labels,fontsize=9); ax.set_xlabel('Count'); ax.set_title({'properties':'Properties by region','customers':'Customers by region','visits':'Visit requests by region'}[kind]);
    for i,v in enumerate(values): ax.text(v,i,f' {v}',va='center',fontsize=9)
    b=io.BytesIO(); fig.savefig(b,format='png',dpi=150,bbox_inches='tight'); plt.close(fig); b.seek(0); b.name=f'{kind}_regions.png'; return b

def _log_lines(where='',params=None,limit=30):
    with db_connect() as c,c.cursor() as cur:
        cur.execute("SELECT a.created_at,COALESCE(u.display_name,u.username,'SYSTEM'),a.action,a.entity_type,a.entity_id,a.severity FROM audit_events a LEFT JOIN users u ON u.id=a.actor_user_id WHERE 1=1 "+where+' ORDER BY a.id DESC LIMIT %s',(params or [])+[limit]); rows=cur.fetchall()
    out=[]
    for dt,user,act,et,eid,sev in rows:
        local=dt.astimezone(TEHRAN).strftime('%m/%d %H:%M'); out.append(f'• {local} | {user} | {act}'+(f' | {et}#{eid}' if et else '')+(f' | {sev}' if sev!='INFO' else ''))
    return '\n'.join(out) or 'لاگی یافت نشد.'
def _alerts(session):
    with db_connect() as c,c.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_events WHERE severity IN('WARNING','ERROR','CRITICAL') AND created_at>NOW()-interval '7 days'"); err=cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM publish_queue WHERE queue_status='FAILED'"); pub=cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM visit_requests WHERE status='SCHEDULED' AND visit_at BETWEEN NOW() AND NOW()+interval '24 hours' AND reminder_sent_at IS NULL"); rem=cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM properties WHERE status IN('INVALID','DUPLICATE')"); bad=cur.fetchone()[0]
    return f'🚨 هشدارها\n\nخطا/هشدار ۷ روز اخیر: {err}\nخطای انتشار: {pub}\nبازدید نزدیک بدون Reminder ثبت‌شده: {rem}\nفایل مشکل‌دار: {bad}'

def _manual_prompt(context):
    idx=context.user_data['manual_idx']; key,label,opt=FIELDS[idx]; kb=[]
    if key=='property_type': kb=[[InlineKeyboardButton(x,callback_data='manual_type_'+v)] for x,v in TYPE_MAP.items()]
    if opt: kb.append([InlineKeyboardButton('⏭ رد کردن',callback_data='manual_skip')])
    kb.append([InlineKeyboardButton('❌ انصراف',callback_data='manual_cancel')])
    if key=='current_price_toman':
        prompt=(
            f'➕ افزودن فایل\n\nمرحله {idx+1} از {len(FIELDS)}\n'
            'قیمت کل را وارد کنید.\n\n'
            'نمونه‌های قابل قبول:\n'
            '• ۴.۵ میلیارد\n'
            '• ۴۵۰۰ میلیون\n'
            '• ۴,۵۰۰,۰۰۰,۰۰۰ تومان\n'
            '• 4.5'
        )
        return prompt,InlineKeyboardMarkup(kb)
    return f'➕ افزودن فایل\n\nمرحله {idx+1} از {len(FIELDS)}\n{label} را وارد کنید:',InlineKeyboardMarkup(kb)
def _preview(data):
    labels={k:l for k,l,_ in FIELDS}
    reverse_types={v:k for k,v in TYPE_MAP.items()}

    def fmt_num(v):
        try:
            f=float(v)
            if f.is_integer():
                return str(int(f))
            return ('%.2f' % f).rstrip('0').rstrip('.')
        except Exception:
            return str(v)

    def fmt_price(toman):
        try:
            n=int(toman)
        except Exception:
            return str(toman)
        if n >= 1_000_000_000:
            b=n/1_000_000_000
            return f"{fmt_num(b)} میلیارد تومان"
        if n >= 1_000_000:
            return f"{fmt_num(n/1_000_000)} میلیون تومان"
        return f"{n:,} تومان"

    lines=['🧾 پیش‌نمایش فایل','']
    area=None
    try:
        area=float(data.get('area_m2')) if data.get('area_m2') not in (None,'',SKIP) else None
    except Exception:
        area=None

    for k,_,_ in FIELDS:
        v=data.get(k)
        if v in (None,'',SKIP):
            continue

        if k=='property_type':
            shown=reverse_types.get(v,v)
        elif k=='current_price_toman':
            shown=fmt_price(v)
        elif k=='area_m2':
            shown=fmt_num(v)
        else:
            shown=v
        lines.append(f'{labels[k]}: {shown}')

    price=data.get('current_price_toman')
    if price not in (None,'',SKIP) and area and area>0:
        try:
            ppm=int(round(int(price)/area))
            lines.append(f'قیمت هر متر: {fmt_price(ppm)}')
        except Exception:
            pass

    lines.append(f"\n📸 تعداد عکس: {len(data.get('photos',[]))}")
    return '\n'.join(lines)
def _preview_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton('✅ تأیید و ثبت در صف انتشار',callback_data='manual_publish')],[InlineKeyboardButton('✏️ ویرایش',callback_data='manual_edit'),InlineKeyboardButton('❌ انصراف',callback_data='manual_cancel')]])
def _photo_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton('✅ پایان ارسال عکس‌ها',callback_data='manual_photos_done'),InlineKeyboardButton('بدون عکس',callback_data='manual_no_photos')],[InlineKeyboardButton('❌ انصراف',callback_data='manual_cancel')]])
def _edit_kb():
    rows=[]
    for i,(_,label,_) in enumerate(FIELDS): rows.append([InlineKeyboardButton(label,callback_data=f'manual_editfield_{i}')])
    rows.append([InlineKeyboardButton('⬅️ پیش‌نمایش',callback_data='manual_preview')]); return InlineKeyboardMarkup(rows)

def _parse_manual_price_toman(value):
    # Accept: 4.5 میلیارد | 4500 میلیون | 4,500,000,000 تومان | bare 4.5
    raw=norm_digits(value).strip().lower()
    raw=(raw.replace('٬',',').replace('٫','.').replace('،',',')
            .replace('تومن','تومان').replace('میلیاردی','میلیارد').replace('میلیونی','میلیون'))
    has_billion='میلیارد' in raw
    has_million='میلیون' in raw
    has_toman='تومان' in raw

    numeric=re.sub(r'[^0-9.,]', '', raw)
    if not numeric:
        raise ValueError('price')

    # Commas are thousands separators. A single comma with 1-2 trailing digits is
    # accepted as a decimal comma only when there are no dots.
    if ',' in numeric and '.' not in numeric:
        parts=numeric.split(',')
        if len(parts)==2 and len(parts[1]) in (1,2) and len(parts[0]) <= 3:
            numeric='.'.join(parts)
        else:
            numeric=''.join(parts)
    else:
        numeric=numeric.replace(',','')

    try:
        n=float(numeric)
    except Exception:
        raise ValueError('price')
    if n <= 0:
        raise ValueError('price')

    if has_billion:
        toman=round(n*1_000_000_000)
    elif has_million:
        toman=round(n*1_000_000)
    elif has_toman:
        toman=round(n)
    else:
        # Friendly shorthand for property prices:
        # 4.5 / 4 / 21.25 => billion toman
        # 4500 / 21250 => million toman
        # 4500000000 => toman
        if n < 1000:
            toman=round(n*1_000_000_000)
        elif n < 100000:
            toman=round(n*1_000_000)
        elif n >= 100_000_000:
            toman=round(n)
        else:
            # Ambiguous bare values are rejected instead of silently corrupting price.
            raise ValueError('price_ambiguous')

    if toman < 100_000_000:
        # Real-estate total prices below 100m are almost certainly an input-unit mistake.
        raise ValueError('price_too_small')
    return int(toman)

def _coerce(key,value):
    if key in ('area_m2',):
        s=norm_digits(value).replace(',','').strip(); return float(s)
    if key in ('bedrooms','total_floors','units_per_floor','build_year'): return clean_int(value)
    if key=='current_price_toman':
        return _parse_manual_price_toman(value)
    if key=='property_type': return TYPE_MAP.get(value,value.upper() if re.fullmatch(r'[A-Za-z_]+',value) else value)
    return value.strip()
def _bool_text(v):
    s=str(v or '').strip();
    if any(x in s for x in ('ندارد','خیر','نیست')): return False
    if any(x in s for x in ('دارد','بله','هست')): return True
    return None

def _create_manual(data,user_id):
    # Never persist the UI sentinel (__SKIP__) into business columns.
    clean={k:(None if v==SKIP else v) for k,v in data.items()}
    clean['photos']=data.get('photos',[])
    price=int(clean['current_price_toman'])
    billion=price/1_000_000_000
    try:
        from property_parser import make_price_hashtag
        hashtag=make_price_hashtag(billion)
    except Exception:
        lower=int(billion); hashtag=f'#آپارتمان_{lower}_{lower+1}'
    raw='\n'.join(f'{l}: {clean.get(k)}' for k,l,_ in FIELDS if clean.get(k) not in (None,''))
    with db_connect() as c,c.cursor() as cur:
        cur.execute("""INSERT INTO properties(property_type,district,address_text,area_m2,bedrooms,floor_text,total_floors,build_year,direction,has_parking,has_elevator,has_storage,current_price_toman,current_price_billion,price_hashtag,raw_text,status,neighborhood,units_per_floor,deed_text,installations_text,parking_text,storage_text,elevator_text,special_notes,source_kind,ingest_status,manual_created_by_user_id,manual_created_at,has_media) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',%s,%s,%s,%s,%s,%s,%s,%s,'MANUAL','MANUAL_READY',%s,NOW(),%s) RETURNING id""",(clean.get('property_type'),clean.get('district'),clean.get('address_text'),clean.get('area_m2'),clean.get('bedrooms'),clean.get('floor_text'),clean.get('total_floors'),clean.get('build_year'),clean.get('direction'),_bool_text(clean.get('parking_text')),_bool_text(clean.get('elevator_text')),_bool_text(clean.get('storage_text')),price,billion,hashtag,raw,clean.get('district'),clean.get('units_per_floor'),clean.get('deed_text'),clean.get('installations_text'),clean.get('parking_text'),clean.get('storage_text'),clean.get('elevator_text'),clean.get('special_notes'),user_id,bool(clean.get('photos')))); pid=cur.fetchone()[0]
        attrs=[('total_floors','تعداد کل طبقات',clean.get('total_floors'),40),('units_per_floor','تعداد واحد در طبقه',clean.get('units_per_floor'),41),('deed','سند',clean.get('deed_text'),70),('installations','نصبیات / امکانات',clean.get('installations_text'),80)]
        # Free-text notes are public listing details, not just a private DB
        # column. Use the same keys as the channel editor so they can be
        # displayed, replaced and cleared there without duplicate copies.
        for index, line in enumerate(str(clean.get('special_notes') or '').splitlines()):
            line = line.strip()
            if line:
                key = 'source_detail_' + hashlib.sha1(line.encode('utf-8')).hexdigest()[:12]
                attrs.append((key, 'جزئیات', line, 90 + index))
        for k,l,v,o in attrs:
            if v not in (None,''): cur.execute("INSERT INTO property_attributes(property_id,attribute_key,attribute_label,attribute_value,display_order,is_visible) VALUES(%s,%s,%s,%s,%s,TRUE) ON CONFLICT(property_id,attribute_key) DO UPDATE SET attribute_value=EXCLUDED.attribute_value,attribute_label=EXCLUDED.attribute_label,is_visible=TRUE,updated_at=NOW()",(pid,k,l,str(v),o))
        for pos,p in enumerate(clean.get('photos',[]),1): cur.execute("INSERT INTO property_media(property_id,telegram_file_id,telegram_file_unique_id,media_type,position) VALUES(%s,%s,%s,'PHOTO',%s) ON CONFLICT DO NOTHING",(pid,p['file_id'],p['unique_id'],pos))
        cur.execute(
            """
            INSERT INTO publish_queue(
                property_id,queue_status,priority,queue_source,queued_by_user_id,created_at,updated_at
            )
            SELECT %s,'READY',100,'MANUAL_STAFF',%s,NOW(),NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM publish_queue
                WHERE property_id=%s
                  AND queue_status IN ('READY','SCHEDULED','PENDING_DUPLICATE','PUBLISHED')
            )
            """,
            (pid,user_id,pid),
        )
        c.commit()
    audit_event(
        user_id,
        'MANUAL_PROPERTY_CREATED',
        entity_type='PROPERTY',
        entity_id=pid,
        details={
            'district':clean.get('district'),
            'price_toman':price,
            'price_billion':billion,
            'queue_priority':100,
            'queue_source':'MANUAL_STAFF',
        },
    )
    return pid


def _set_manual_duplicate_queue_state(property_id, status):
    with db_connect() as c,c.cursor() as cur:
        cur.execute(
            """
            UPDATE publish_queue
            SET queue_status=%s, priority=100, queue_source='MANUAL_STAFF',
                scheduled_for=NULL, last_error=NULL, updated_at=NOW()
            WHERE property_id=%s
              AND queue_status <> 'PUBLISHED'
            """,
            (status,property_id),
        )
        c.commit()


def _next_schedule_window_start(now=None):
    now=now or datetime.now(TEHRAN)
    with db_connect() as c,c.cursor() as cur:
        cur.execute(
            """
            SELECT weekday,start_time
            FROM schedule_windows
            WHERE is_active=TRUE
            ORDER BY weekday,start_time,id
            """
        )
        rows=cur.fetchall()
    candidates=[]
    for day_offset in range(0,8):
        target_date=(now+timedelta(days=day_offset)).date()
        target_weekday=target_date.weekday()
        for weekday,start_time in rows:
            if weekday != target_weekday:
                continue
            start_dt=datetime.combine(target_date,start_time,tzinfo=TEHRAN)
            if start_dt > now:
                candidates.append(start_dt)
    return min(candidates) if candidates else None


def _next_schedule_window_label(now=None):
    now=now or datetime.now(TEHRAN)
    next_start=_next_schedule_window_start(now)
    if not next_start:
        return 'اولین بازه مجاز بعدی'
    clock=next_start.strftime('%H:%M')
    days=(next_start.date()-now.date()).days
    if days==0:
        return f'امروز ساعت {clock}'
    if days==1:
        return f'فردا ساعت {clock}'
    weekday_names=['دوشنبه','سه‌شنبه','چهارشنبه','پنجشنبه','جمعه','شنبه','یکشنبه']
    return f"{weekday_names[next_start.weekday()]} ساعت {clock}"


def _manual_queue_confirmation_text(property_id):
    # Lazy import avoids changing module import order in bot.py.
    from scheduler import get_active_window
    now=datetime.now(TEHRAN)
    window=get_active_window(now)
    base=(
        '✅ فایل شما با موفقیت ثبت شد و با اولویت بالا در صف انتشار قرار گرفت.\n\n'
        f'🧾 شماره پیگیری: صف {property_id}\n'
        '⭐ اولویت صف: فایل ثبت‌شده توسط مشاور/مدیر'
    )
    if window:
        return (
            base
            + '\n\n⏰ الان داخل بازه استاندارد انتشار کانال هستیم؛ '
              'فایل در اولین نوبت مجاز Scheduler منتشر می‌شود.'
        )
    return (
        base
        + '\n\n🌙 الان خارج از ساعت استاندارد انتشار کانال هستیم. '
          f'فایل شما محفوظ است و در {_next_schedule_window_label(now)}، '
          'قبل از فایل‌های عادی صف بررسی می‌شود.'
    )

async def handle_photo(update,context,session):
    if context.user_data.get('manual_phase')!='photos' or not update.message or not update.message.photo:return False
    p=update.message.photo[-1]; arr=context.user_data.setdefault('manual_data',{}).setdefault('photos',[])
    if p.file_unique_id not in {x['unique_id'] for x in arr}: arr.append({'file_id':p.file_id,'unique_id':p.file_unique_id,'media_group_id':update.message.media_group_id})
    await update.message.reply_text(f'📸 عکس دریافت شد. مجموع: {len(arr)}',reply_markup=_photo_kb()); return True

async def handle_text(update,context,session):
    phase=context.user_data.get('manual_phase')
    if phase not in ('fields','edit'): return False
    if not update.message or not update.message.text:return False
    if phase=='edit': idx=context.user_data.get('manual_edit_idx')
    else: idx=context.user_data.get('manual_idx',0)
    key,label,opt=FIELDS[idx]
    try: val=_coerce(key,update.message.text)
    except Exception:
        if key=='current_price_toman':
            await update.message.reply_text(
                '❌ قیمت قابل تشخیص نیست.\n'
                'مثلاً یکی از این مدل‌ها را بفرستید:\n'
                '۴.۵ میلیارد\n۴۵۰۰ میلیون\n۴,۵۰۰,۰۰۰,۰۰۰ تومان\n4.5'
            )
        else:
            await update.message.reply_text(f'❌ مقدار «{label}» معتبر نیست. دوباره ارسال کنید.')
        return True
    context.user_data.setdefault('manual_data',{})[key]=val
    if phase=='edit':
        context.user_data['manual_phase']='preview'; await update.message.reply_text(_preview(context.user_data['manual_data']),reply_markup=_preview_kb()); return True
    context.user_data['manual_idx']=idx+1
    if idx+1>=len(FIELDS):
        context.user_data['manual_phase']='photos'; await update.message.reply_text('📸 عکس‌های واحد را در صورت وجود ارسال کنید.\nمی‌توانید چند عکس یا Album / Media Group بفرستید.',reply_markup=_photo_kb()); return True
    text,kb=_manual_prompt(context); await update.message.reply_text(text,reply_markup=kb); return True


PDF_PRICE_PRESETS = {
    'export_properties_pdf_all':   (None, None, 'همه قیمت‌ها'),
    'export_properties_pdf_u5':    (None, 5, 'تا ۵ میلیارد تومان'),
    'export_properties_pdf_5_10':  (5, 10, '۵ تا ۱۰ میلیارد تومان'),
    'export_properties_pdf_10_15': (10, 15, '۱۰ تا ۱۵ میلیارد تومان'),
    'export_properties_pdf_15_20': (15, 20, '۱۵ تا ۲۰ میلیارد تومان'),
    'export_properties_pdf_20_30': (20, 30, '۲۰ تا ۳۰ میلیارد تومان'),
    'export_properties_pdf_30p':   (30, None, 'بالای ۳۰ میلیارد تومان'),
}


def _pdf_price_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📋 همه فایل‌ها', callback_data='export_properties_pdf_all')],
        [
            InlineKeyboardButton('💰 تا ۵ میلیارد', callback_data='export_properties_pdf_u5'),
            InlineKeyboardButton('💰 ۵ تا ۱۰ میلیارد', callback_data='export_properties_pdf_5_10'),
        ],
        [
            InlineKeyboardButton('💰 ۱۰ تا ۱۵ میلیارد', callback_data='export_properties_pdf_10_15'),
            InlineKeyboardButton('💰 ۱۵ تا ۲۰ میلیارد', callback_data='export_properties_pdf_15_20'),
        ],
        [
            InlineKeyboardButton('💰 ۲۰ تا ۳۰ میلیارد', callback_data='export_properties_pdf_20_30'),
            InlineKeyboardButton('💰 بالای ۳۰ میلیارد', callback_data='export_properties_pdf_30p'),
        ],
        [InlineKeyboardButton('✍️ بازه دلخواه', callback_data='export_properties_pdf_custom')],
        [InlineKeyboardButton('⬅️ منوی اصلی', callback_data='home')],
    ])


def _fa_billion_value(value):
    value = float(value)
    text = f'{value:.2f}'.rstrip('0').rstrip('.')
    return fa_num(text)


def _parse_pdf_price_range(text):
    """
    (min, max) in billions from «۹ تا ۱۱ میلیارد», «۱۲ ۱۸», «زیر ۸» or
    «بالای ۲۰» (None for an open end); None when the text is not a range.
    """

    raw = norm_digits(text).replace('٫', '.').replace('،', ' ').replace(',', ' ')
    numbers = [float(n) for n in re.findall(r'\d+(?:\.\d+)?', raw)]
    if len(numbers) == 2:
        low, high = sorted(numbers)
        return (low, high) if high > 0 else None
    if len(numbers) == 1 and numbers[0] > 0:
        if re.search(r'زیر|کمتر|حداکثر|سقف|^\s*تا', raw):
            return None, numbers[0]
        if re.search(r'بالا|بیشتر|حداقل', raw):
            return numbers[0], None
    return None


def _pdf_price_label(min_billion, max_billion):
    if min_billion is None:
        return f'تا {_fa_billion_value(max_billion)} میلیارد تومان'
    if max_billion is None:
        return f'بالای {_fa_billion_value(min_billion)} میلیارد تومان'
    return f'{_fa_billion_value(min_billion)} تا {_fa_billion_value(max_billion)} میلیارد تومان'


async def _send_property_pdf(update, context, session, min_billion=None, max_billion=None, label='همه قیمت‌ها'):
    min_toman = None if min_billion is None else int(round(float(min_billion) * 1_000_000_000))
    max_toman = None if max_billion is None else int(round(float(max_billion) * 1_000_000_000))

    status_text = (
        '⏳ در حال ساخت PDF فایل‌های فعال + صف انتشار...\n'
        f'💰 بازه قیمت: {label}\n'
        'فایل‌ها از ارزان‌ترین به گران‌ترین مرتب می‌شوند.'
    )

    q = getattr(update, 'callback_query', None)

    if q:
        await q.edit_message_text(status_text)
    else:
        await update.message.reply_text(status_text)

    try:
        from report_export import build_property_pdf
        from telegram import InputFile

        pdf_bytes, filename, count = await asyncio.to_thread(
            build_property_pdf,
            min_toman,
            max_toman,
            label,
        )

        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=InputFile(pdf_bytes, filename=filename),
            caption=(
                '📄 لیست فایل‌های فعال + صف انتشار\n'
                f'💰 بازه قیمت: {label}\n'
                f'تعداد: {fa_num(count)}\n'
                'مرتب‌شده بر اساس قیمت کل'
            ),
        )

        audit_event(
            session[0],
            'PROPERTY_PDF_EXPORTED',
            entity_type='REPORT',
            details={
                'property_count': count,
                'filename': filename,
                'price_label': label,
                'min_price_toman': min_toman,
                'max_price_toman': max_toman,
            },
        )

        success_text = (
            '✅ PDF ساخته و ارسال شد.\n'
            f'💰 بازه قیمت: {label}\n'
            f'تعداد فایل: {fa_num(count)}'
        )

        success_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('📄 انتخاب بازه دیگر', callback_data='export_properties_pdf')],
            [InlineKeyboardButton('⬅️ منوی اصلی', callback_data='home')],
        ])

        if q:
            await q.edit_message_text(success_text, reply_markup=success_kb)
        else:
            await update.message.reply_text(success_text, reply_markup=success_kb)

    except Exception as exc:
        logging.exception('PDF export failed')

        audit_event(
            session[0],
            'PROPERTY_PDF_EXPORT_FAILED',
            entity_type='REPORT',
            severity='ERROR',
            details={
                'error': str(exc)[:2000],
                'price_label': label,
                'min_price_toman': min_toman,
                'max_price_toman': max_toman,
            },
        )

        error_text = '❌ ساخت PDF انجام نشد.\n' + str(exc)[:1200]
        error_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('📄 بازگشت به بازه‌ها', callback_data='export_properties_pdf')],
            [InlineKeyboardButton('⬅️ منوی اصلی', callback_data='home')],
        ])

        if q:
            await q.edit_message_text(error_text, reply_markup=error_kb)
        else:
            await update.message.reply_text(error_text, reply_markup=error_kb)


async def handle_button(update,context,session):
    q=update.callback_query; a=q.data or ''
    if a=='stats': await q.edit_message_text('📊 داشبورد آمار حرفه‌ای',reply_markup=stats_menu()); return True
    if a.startswith('stats_'):
        if a=='stats_overview': text=_overview(session)
        elif a=='stats_regions': text=_regions(session)
        elif a=='stats_customers': text=_customers(session)
        elif a=='stats_files': text=_files(session)
        elif a=='stats_visits': text=_visits(session)
        elif a=='stats_advisors': text=_advisors(session)
        elif a=='stats_sources': text=_sources(session)
        else:return False
        kb=stats_menu()
        if a=='stats_regions': kb=InlineKeyboardMarkup([[InlineKeyboardButton('📊 نمودار فایل‌ها',callback_data='stats_chart_properties'),InlineKeyboardButton('📊 نمودار مشتری',callback_data='stats_chart_customers')],[InlineKeyboardButton('📊 نمودار بازدید',callback_data='stats_chart_visits')],[InlineKeyboardButton('⬅️ آمار',callback_data='stats')]])
        await q.edit_message_text(text[:4000],reply_markup=kb); return True
    if a.startswith('stats_chart_'):
        kind=a.replace('stats_chart_',''); b=make_chart(kind,session)
        if not b: await q.answer('داده‌ای برای نمودار نیست.',show_alert=True)
        else: await context.bot.send_photo(chat_id=update.effective_chat.id,photo=b,caption='📊 نمودار میله‌ای بر اساس داده‌های ساختاریافته')
        return True
    if a=='export_properties_pdf':
        if not _has_perm(session,'reports_export'):
            await q.answer('دسترسی ندارید.',show_alert=True)
            return True

        # A range typed while this menu is open works without «بازه دلخواه».
        context.user_data['pdf_price_custom'] = 'menu'

        await q.edit_message_text(
            '📄 دانلود PDF فایل‌های فعال\n\n'
            'بازه قیمتی موردنظر را انتخاب کنید\n'
            'یا بنویسید؛ مثلاً «۹ تا ۱۱»، «زیر ۸» یا «بالای ۲۰».',
            reply_markup=_pdf_price_menu(),
        )
        return True

    if a in PDF_PRICE_PRESETS:
        if not _has_perm(session,'reports_export'):
            await q.answer('دسترسی ندارید.',show_alert=True)
            return True

        context.user_data.pop('pdf_price_custom', None)

        min_billion, max_billion, label = PDF_PRICE_PRESETS[a]

        await _send_property_pdf(
            update,
            context,
            session,
            min_billion=min_billion,
            max_billion=max_billion,
            label=label,
        )
        return True

    if a=='export_properties_pdf_custom':
        if not _has_perm(session,'reports_export'):
            await q.answer('دسترسی ندارید.',show_alert=True)
            return True

        context.user_data['pdf_price_custom'] = True

        await q.edit_message_text(
            '✍️ بازه دلخواه قیمت\n\n'
            'حداقل و حداکثر قیمت را به میلیارد تومان در یک پیام ارسال کنید.\n\n'
            'مثال:\n'
            '۱۲ ۱۸\n\n'
            'یا:\n'
            '۱۲.۵ تا ۱۸\n'
            'یا: زیر ۸ / بالای ۲۰\n\n'
            'برای مثال بالا، فقط فایل‌های ۱۲ تا ۱۸ میلیارد وارد PDF می‌شوند.',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('⬅️ بازگشت به بازه‌ها', callback_data='export_properties_pdf')]
            ]),
        )
        return True

    if a=='logs': await q.edit_message_text('🧾 مرکز لاگ و Audit',reply_markup=logs_menu()); return True
    if a=='logs_recent': await q.edit_message_text('🕘 آخرین فعالیت‌ها\n\n'+_log_lines(),reply_markup=logs_menu()); return True
    if a=='logs_users':
        with db_connect() as c,c.cursor() as cur: cur.execute("SELECT id,COALESCE(display_name,username) FROM users ORDER BY role,id LIMIT 40"); rows=cur.fetchall()
        kb=InlineKeyboardMarkup([[InlineKeyboardButton(name[:40],callback_data=f'logs_user_{uid}')] for uid,name in rows]+[[InlineKeyboardButton('⬅️ لاگ‌ها',callback_data='logs')]])
        await q.edit_message_text('👤 کاربر را انتخاب کنید:',reply_markup=kb); return True
    if a.startswith('logs_user_'):
        uid=int(a.rsplit('_',1)[1]); await q.edit_message_text('👤 لاگ کاربر\n\n'+_log_lines('AND a.actor_user_id=%s',[uid],50),reply_markup=logs_menu()); return True
    if a=='logs_types':
        with db_connect() as c,c.cursor() as cur: cur.execute('SELECT action,count(*) FROM audit_events GROUP BY action ORDER BY 2 DESC LIMIT 30'); rows=cur.fetchall()
        kb=InlineKeyboardMarkup([[InlineKeyboardButton(f'{x} ({n})'[:60],callback_data='logs_action_'+x)] for x,n in rows]+[[InlineKeyboardButton('⬅️ لاگ‌ها',callback_data='logs')]])
        await q.edit_message_text('🧩 نوع عملیات را انتخاب کنید:',reply_markup=kb); return True
    if a.startswith('logs_action_'):
        act=a.replace('logs_action_','',1); await q.edit_message_text('🧩 '+act+'\n\n'+_log_lines('AND a.action=%s',[act],50),reply_markup=logs_menu()); return True
    if a=='logs_range': await q.edit_message_text('📆 ۷ روز اخیر\n\n'+_log_lines("AND a.created_at >= NOW()-interval '7 days'",limit=50),reply_markup=logs_menu()); return True
    if a=='logs_alerts': await q.edit_message_text(_alerts(session),reply_markup=logs_menu()); return True
    if a in ('logs_property_prompt','logs_customer_prompt'):
        context.user_data['final_log_prompt']='PROPERTY' if 'property' in a else 'CUSTOMER'; await q.edit_message_text('شناسه عددی را ارسال کنید:'); return True
    if a=='manual_add':
        if not _has_perm(session,'manual_property_add'): await q.answer('دسترسی ندارید.',show_alert=True); return True
        context.user_data.clear(); context.user_data.update({'manual_phase':'fields','manual_idx':0,'manual_data':{'photos':[]}}); text,kb=_manual_prompt(context); await q.edit_message_text(text,reply_markup=kb); return True
    if a=='manual_cancel': context.user_data.clear(); await q.edit_message_text('❌ افزودن فایل لغو شد.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ منوی اصلی',callback_data='home')]])); return True
    if a=='manual_skip':
        if context.user_data.get('manual_phase')!='fields':return True
        idx=context.user_data['manual_idx']; key,_,opt=FIELDS[idx]
        if not opt: await q.answer('این فیلد الزامی است.',show_alert=True); return True
        context.user_data['manual_data'][key]=SKIP; context.user_data['manual_idx']=idx+1
        if idx+1>=len(FIELDS): context.user_data['manual_phase']='photos'; await q.edit_message_text('📸 عکس‌های واحد را در صورت وجود ارسال کنید.',reply_markup=_photo_kb())
        else: text,kb=_manual_prompt(context); await q.edit_message_text(text,reply_markup=kb)
        return True
    if a.startswith('manual_type_'):
        context.user_data['manual_data']['property_type']=a.replace('manual_type_',''); context.user_data['manual_idx']=1; text,kb=_manual_prompt(context); await q.edit_message_text(text,reply_markup=kb); return True
    if a in ('manual_photos_done','manual_no_photos'):
        if a=='manual_no_photos': context.user_data['manual_data']['photos']=[]
        context.user_data['manual_phase']='preview'; await q.edit_message_text(_preview(context.user_data['manual_data']),reply_markup=_preview_kb()); return True
    if a=='manual_preview': context.user_data['manual_phase']='preview'; await q.edit_message_text(_preview(context.user_data['manual_data']),reply_markup=_preview_kb()); return True
    if a=='manual_edit': await q.edit_message_text('✏️ کدام مورد ویرایش شود؟',reply_markup=_edit_kb()); return True
    if a.startswith('manual_editfield_'):
        idx=int(a.rsplit('_',1)[1]); context.user_data['manual_phase']='edit'; context.user_data['manual_edit_idx']=idx; _,label,_=FIELDS[idx]; await q.edit_message_text(f'✏️ مقدار جدید «{label}» را ارسال کنید:'); return True
    if a in ('manual_publish','manual_dup_publish'):
        data=context.user_data.get('manual_data')
        if not data:
            await q.answer('اطلاعات این فرم از بین رفته؛ دوباره شروع کنید.',show_alert=True)
            return True
        pid=context.user_data.get('manual_property_id')
        if not pid:
            pid=_create_manual(data,session[0])
            context.user_data['manual_property_id']=pid
            from duplicate_detector import detect_and_store_duplicate
            dup=detect_and_store_duplicate(pid)
            if dup and a!='manual_dup_publish':
                _set_manual_duplicate_queue_state(pid,'PENDING_DUPLICATE')
                context.user_data['manual_duplicate']=dup
                await q.edit_message_text(
                    f"⚠️ فایل مشابه پیدا شد.\nشناسه فایل مشابه: {dup.get('duplicate_of_property_id')}\nامتیاز شباهت: {dup.get('similarity_score')}\nدلیل: {dup.get('match_reason')}\n\nاگر تأیید کنید، فایل با اولویت مشاور در صف استاندارد انتشار قرار می‌گیرد.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton('✅ با وجود شباهت در صف قرار بگیرد',callback_data='manual_dup_publish')],
                        [InlineKeyboardButton('❌ انصراف',callback_data='manual_cancel_draft')],
                    ]),
                )
                return True
        if a=='manual_dup_publish':
            from duplicate_detector import set_duplicate_review_status
            set_duplicate_review_status(pid,'APPROVED')
            _set_manual_duplicate_queue_state(pid,'READY')

        audit_event(
            session[0],
            'MANUAL_PROPERTY_PRIORITY_QUEUED',
            entity_type='PROPERTY',
            entity_id=pid,
            details={'priority':100,'queue_source':'MANUAL_STAFF'},
        )
        message=_manual_queue_confirmation_text(pid)
        context.user_data.clear()
        await q.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ منوی اصلی',callback_data='home')]]),
        )
        return True
    if a=='manual_cancel_draft':
        pid=context.user_data.get('manual_property_id')
        if pid:
            with db_connect() as c,c.cursor() as cur:
                cur.execute("UPDATE properties SET status='INVALID',archived_at=NOW(),updated_at=NOW() WHERE id=%s AND published_message_id IS NULL",(pid,))
                cur.execute("UPDATE publish_queue SET queue_status='SKIPPED',last_error='Cancelled by operator',updated_at=NOW() WHERE property_id=%s AND queue_status <> 'PUBLISHED'",(pid,))
                c.commit()
            audit_event(session[0],'MANUAL_PROPERTY_CANCELLED',entity_type='PROPERTY',entity_id=pid)
        context.user_data.clear(); await q.edit_message_text('❌ ثبت فایل لغو شد.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ منوی اصلی',callback_data='home')]])); return True
    return False

async def handle_aux_text(update,context,session):
    mode = context.user_data.get('pdf_price_custom')
    if mode:
        if not update.message or not update.message.text:
            return False

        price_range = _parse_pdf_price_range(update.message.text)
        if not price_range:
            if mode == 'menu':
                # Not a price: the text is for something else.
                context.user_data.pop('pdf_price_custom', None)
                return False
            await update.message.reply_text(
                '❌ بازه قابل تشخیص نیست.\n\n'
                'حداقل و حداکثر را در یک پیام بفرستید.\n'
                'مثال: ۱۲ ۱۸\n'
                'یا: ۱۲.۵ تا ۱۸\n'
                'یا: زیر ۸ / بالای ۲۰'
            )
            return True

        context.user_data.pop('pdf_price_custom', None)
        if not _has_perm(session,'reports_export'):
            await update.message.reply_text('⛔ دسترسی ندارید.')
            return True

        min_billion, max_billion = price_range
        await _send_property_pdf(
            update,
            context,
            session,
            min_billion=min_billion,
            max_billion=max_billion,
            label=_pdf_price_label(min_billion, max_billion),
        )
        return True

    typ=context.user_data.pop('final_log_prompt',None)
    if not typ:return False

    n=clean_int(update.message.text)

    if not n:
        await update.message.reply_text('شناسه معتبر نیست.')
        return True

    await update.message.reply_text(
        f'🧾 لاگ‌های {typ} #{n}\n\n'
        + _log_lines(
            'AND a.entity_type=%s AND a.entity_id=%s',
            [typ,n],
            50,
        ),
        reply_markup=logs_menu(),
    )
    return True


async def error_handler(update, context):
    try:
        uid=None
        if update and getattr(update,'effective_user',None):
            with db_connect() as c,c.cursor() as cur:
                cur.execute("SELECT u.id FROM user_sessions s JOIN users u ON u.id=s.user_id WHERE s.telegram_user_id=%s AND s.is_active=TRUE LIMIT 1",(update.effective_user.id,)); r=cur.fetchone(); uid=r[0] if r else None
        audit_event(uid,'SYSTEM_ERROR',entity_type='BOT',severity='ERROR',details={'error':str(context.error)[:3000]})
    except Exception:
        logging.exception('error audit failed')

