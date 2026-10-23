import json
import os
import re
import secrets
import psycopg
from psycopg.rows import dict_row


def connect():
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        dbname=os.getenv("POSTGRES_DB"), user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"), row_factory=dict_row,
    )


def one(sql, params=()):
    with connect() as c, c.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def all_rows(sql, params=()):
    with connect() as c, c.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def execute(sql, params=()):
    with connect() as c, c.cursor() as cur:
        cur.execute(sql, params)
        c.commit()


def audit(action, actor=None, agency_id=None, entity_type=None, entity_id=None, details=None):
    execute("""INSERT INTO partner_audit_log(agency_id,actor_telegram_id,action,entity_type,entity_id,details)
               VALUES(%s,%s,%s,%s,%s,%s::jsonb)""",
            (agency_id, actor, action, entity_type, entity_id, json.dumps(details or {}, ensure_ascii=False)))


def member(telegram_id):
    return one("""SELECT m.*,a.status agency_status,a.brand_name,a.legal_name
                  FROM partner_members m JOIN partner_agencies a ON a.id=m.agency_id
                  WHERE m.telegram_user_id=%s AND m.is_active=TRUE""", (telegram_id,))


def brand(agency_id):
    return one("SELECT * FROM partner_brand_settings WHERE agency_id=%s", (agency_id,)) or {}


def bot_agency(bot_id):
    if not bot_id: return None
    return one("""SELECT a.* FROM partner_bot_connections b
                  JOIN partner_agencies a ON a.id=b.agency_id
                  WHERE b.bot_id=%s AND b.status='ACTIVE' AND a.status='ACTIVE'""", (bot_id,))


def slugify(text):
    latin = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (latin or "agency") + "-" + secrets.token_hex(3)


def tracking_code(agency_id):
    return f"A{agency_id}-{secrets.token_hex(4).upper()}"


def table_columns(table):
    rows = all_rows("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,))
    return {r['column_name'] for r in rows}


def ensure_partner_v2_schema():
    """Idempotent team-management and three-day availability upgrade."""
    statements = (
        """
        ALTER TABLE partner_members
          ADD COLUMN IF NOT EXISTS can_manage_team BOOLEAN NOT NULL DEFAULT FALSE,
          ADD COLUMN IF NOT EXISTS can_view_stats BOOLEAN NOT NULL DEFAULT FALSE
        """,
        """
        ALTER TABLE partner_invites
          ADD COLUMN IF NOT EXISTS is_revoked BOOLEAN NOT NULL DEFAULT FALSE
        """,
        """
        ALTER TABLE partner_submissions
          ADD COLUMN IF NOT EXISTS last_availability_confirmed_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS availability_check_due_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS availability_check_sent_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS availability_reminder_sent_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS availability_missed_cycles INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS availability_paused_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS status_before_availability_pause VARCHAR(30),
          ADD COLUMN IF NOT EXISTS availability_last_error TEXT
        """,
        """
        UPDATE partner_members
        SET can_manage_team = TRUE,
            can_view_stats = TRUE
        WHERE role IN ('AGENCY_OWNER', 'AGENCY_ADMIN')
        """,
        """
        UPDATE partner_submissions
        SET last_availability_confirmed_at = COALESCE(
                last_availability_confirmed_at,
                NOW()
            ),
            availability_check_due_at = COALESCE(
                availability_check_due_at,
                NOW() + INTERVAL '3 days'
            )
        WHERE status = 'ACTIVE'
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_partner_availability_due
        ON partner_submissions(status, availability_check_due_at)
        """,
    )

    with connect() as c, c.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE partner_members
            DROP CONSTRAINT IF EXISTS partner_members_role_check
            """
        )
        cur.execute(
            """
            ALTER TABLE partner_members
            ADD CONSTRAINT partner_members_role_check
            CHECK (
                role IN (
                    'AGENCY_OWNER',
                    'AGENCY_ADMIN',
                    'ADVISOR',
                    'SECRETARY'
                )
            )
            """
        )
        for statement in statements:
            cur.execute(statement)
        c.commit()


def ensure_partner_v3_schema():
    """Idempotent: deleting files, the agency posting schedule, album message ids."""
    with connect() as c, c.cursor() as cur:
        cur.execute("ALTER TABLE partner_submissions DROP CONSTRAINT IF EXISTS partner_submissions_status_check")
        cur.execute(
            """
            ALTER TABLE partner_submissions
            ADD CONSTRAINT partner_submissions_status_check
            CHECK (status IN ('DRAFT','SUBMITTED','UNDER_REVIEW','APPROVED','REJECTED',
                              'ACTIVE','SOLD','INACTIVE','DELETED'))
            """
        )
        cur.execute("ALTER TABLE partner_submissions ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE partner_publication_jobs ADD COLUMN IF NOT EXISTS telegram_message_ids BIGINT[]")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS partner_publish_schedules (
                agency_id BIGINT PRIMARY KEY REFERENCES partner_agencies(id) ON DELETE CASCADE,
                slot_times TEXT[] NOT NULL DEFAULT '{}',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        c.commit()


def _feature(value):
    """«دارد» / «ندارد» typed by the agent -> True / False (None when unclear)."""
    text = str(value or "").strip()
    if any(word in text for word in ("ندارد", "خیر", "نیست")): return False
    if any(word in text for word in ("دارد", "بله", "هست")): return True
    return None


def price_hashtag(price_toman):
    """ShahFile's price hashtag, e.g. «#آپارتمان_12_13» for 12.5 billion."""
    if not price_toman: return None
    lower = int(price_toman / 1_000_000_000)
    return f"#آپارتمان_{lower}_{lower + 1}"


PUBLIC_TEXT_FIELDS = (
    ("district", "منطقه"), ("address_text", "آدرس"), ("area_m2", "متراژ"), ("bedrooms", "تعداد خواب"),
    ("floor_text", "طبقه"), ("total_floors", "تعداد کل طبقات"), ("units_per_floor", "تعداد واحد در طبقه"),
    ("build_year", "سال ساخت"), ("direction", "جهت"), ("parking_text", "پارکینگ"),
    ("elevator_text", "آسانسور"), ("storage_text", "انباری"), ("deed_text", "سند"),
    ("installations_text", "نصبیات / امکانات"),
)
# Shown in ShahFile's post like the details of a file staff add by hand.
PUBLIC_ATTRIBUTES = (
    ("total_floors", "تعداد کل طبقات", "total_floors", 40),
    ("units_per_floor", "تعداد واحد در طبقه", "units_per_floor", 41),
    ("deed", "سند", "deed_text", 70),
    ("installations", "نصبیات / امکانات", "installations_text", 80),
)


def push_to_central(submission_id, actor_telegram_id, media=None, priority=100):
    """Create one central property safely across old/new schemas and enqueue it.

    media is the photos as ShahFile's own bot stored them ({file_id, unique_id});
    the agency's file ids belong to another bot and cannot be posted by it.
    Private owner fields and the agent's free-text notes (which may carry the
    agency's own contact) are never copied.
    """
    s = one("SELECT * FROM partner_submissions WHERE id=%s", (submission_id,))
    if not s: raise ValueError("submission not found")
    if s.get("central_property_id"): return s["central_property_id"]
    media = list(media or [])
    price_toman = s["current_price_toman"]
    pcols = table_columns("properties")
    values = {
        "property_type": s["property_type"], "district": s["district"],
        "neighborhood": s["district"], "address_text": s["address_text"],
        "area_m2": s["area_m2"], "bedrooms": s["bedrooms"], "floor_text": s["floor_text"],
        "total_floors": s["total_floors"], "units_per_floor": s["units_per_floor"],
        "build_year": s["build_year"], "direction": s["direction"],
        "parking_text": s["parking_text"], "elevator_text": s["elevator_text"],
        "storage_text": s["storage_text"], "deed_text": s["deed_text"],
        "installations_text": s["installations_text"],
        "has_parking": _feature(s["parking_text"]), "has_elevator": _feature(s["elevator_text"]),
        "has_storage": _feature(s["storage_text"]),
        "current_price_toman": price_toman,
        "current_price_billion": (price_toman or 0) / 1_000_000_000,
        "price_hashtag": price_hashtag(price_toman),
        "status": "ACTIVE", "source_kind": "PARTNER", "ingest_status": "MANUAL_READY",
        "has_media": bool(media),
        "raw_text": "\n".join(
            [f"ورودی همکار | کد {s['tracking_code']}"]
            + [f"{label}: {s[key]}" for key, label in PUBLIC_TEXT_FIELDS if s.get(key) not in (None, "")]
        ),
    }
    cols = [k for k,v in values.items() if k in pcols and v is not None]
    placeholders = ",".join(["%s"]*len(cols))
    with connect() as c, c.cursor() as cur:
        cur.execute(f"INSERT INTO properties({','.join(cols)}) VALUES({placeholders}) RETURNING id", [values[k] for k in cols])
        pid = cur.fetchone()["id"]
        for position, photo in enumerate(media, 1):
            cur.execute("""INSERT INTO property_media(property_id,telegram_file_id,telegram_file_unique_id,media_type,position)
                           VALUES(%s,%s,%s,'PHOTO',%s) ON CONFLICT DO NOTHING""",
                        (pid, photo["file_id"], photo["unique_id"], position))
        cur.execute("SELECT to_regclass('property_attributes') IS NOT NULL AS ok")
        if cur.fetchone()["ok"]:
            for key, label, field, order in PUBLIC_ATTRIBUTES:
                if s.get(field) not in (None, ""):
                    cur.execute("""INSERT INTO property_attributes(property_id,attribute_key,attribute_label,attribute_value,display_order,is_visible)
                                   VALUES(%s,%s,%s,%s,%s,TRUE) ON CONFLICT DO NOTHING""",
                                (pid, key, label, str(s[field]), order))
        qcols = table_columns("publish_queue")
        qvalues = {"property_id":pid,"queue_status":"READY","priority":priority,"queue_source":"PARTNER","queued_by_user_id":None}
        qc = [k for k,v in qvalues.items() if k in qcols and v is not None]
        cur.execute(f"INSERT INTO publish_queue({','.join(qc)}) VALUES({','.join(['%s']*len(qc))})", [qvalues[k] for k in qc])
        cur.execute(
            """UPDATE partner_submissions
               SET central_property_id=%s,
                   status='ACTIVE',
                   approved_at=NOW(),
                   approved_by_telegram_id=%s,
                   last_availability_confirmed_at=NOW(),
                   availability_check_due_at=NOW()+interval '3 days',
                   availability_check_sent_at=NULL,
                   availability_reminder_sent_at=NULL,
                   availability_missed_cycles=0,
                   availability_paused_at=NULL,
                   updated_at=NOW()
               WHERE id=%s""",
            (pid,actor_telegram_id,submission_id),
        )
        c.commit()
    audit("CENTRAL_QUEUE_CREATED",actor_telegram_id,s["agency_id"],"SUBMISSION",submission_id,{"property_id":pid,"photos":len(media)})
    return pid


def ensure_central_schema():
    """Idempotent: what the agency changes on a file ShahFile's bot must follow."""
    with connect() as c, c.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS partner_central_events (
                id BIGSERIAL PRIMARY KEY,
                property_id BIGINT NOT NULL,
                event_type VARCHAR(20) NOT NULL
                  CHECK (event_type IN ('DELETED','SOLD','PRICE','STATUS')),
                old_price_toman BIGINT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                processed_at TIMESTAMPTZ
            )
            """
        )
        cur.execute(
            """CREATE INDEX IF NOT EXISTS partner_central_events_pending
               ON partner_central_events(id) WHERE processed_at IS NULL"""
        )
        c.commit()


def central_event(property_id, event_type, old_price_toman=None):
    """Tell ShahFile's bot to bring its channel post in line with the agency's change."""
    if property_id:
        execute(
            "INSERT INTO partner_central_events(property_id,event_type,old_price_toman) VALUES(%s,%s,%s)",
            (property_id, event_type, old_price_toman),
        )
