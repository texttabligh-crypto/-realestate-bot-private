from __future__ import annotations
import re

import html
import io
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import arabic_reshaper
from bidi.algorithm import get_display

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

DB_HOST = os.getenv("POSTGRES_HOST", "postgres")
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")
TEHRAN = ZoneInfo("Asia/Tehran")

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_REGULAR = FONT_DIR / "IRANSansWeb.ttf"
FONT_MEDIUM = FONT_DIR / "IRANSansWeb_Medium.ttf"
FONT_BLACK = FONT_DIR / "IRANSansWeb_Black.ttf"

TYPE_LABELS = {
    "APARTMENT": "آپارتمان",
    "VILLA": "ویلایی",
    "LAND": "زمین",
    "COMMERCIAL": "تجاری",
    "OFFICE": "اداری",
}

# === ACTIVE + QUEUE PDF V1 ===
REPORT_STATUS_LABELS = {
    "PUBLISHED": "منتشرشده",
    "NEW": "جدید در صف",
    "PARSED": "پردازش‌شده",
    "READY": "آماده انتشار",
    "SCHEDULED": "زمان‌بندی‌شده",
    "PENDING_DUPLICATE": "بررسی تکراری",
    "FAILED": "خطادار",
}


FA_TRANS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _db():
    return psycopg.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def _register_fonts():
    required = (FONT_REGULAR, FONT_MEDIUM, FONT_BLACK)
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise RuntimeError("IRANSans font files missing: " + ", ".join(missing))

    registered = set(pdfmetrics.getRegisteredFontNames())
    if "IRANSans" not in registered:
        pdfmetrics.registerFont(TTFont("IRANSans", str(FONT_REGULAR)))
    if "IRANSansMedium" not in registered:
        pdfmetrics.registerFont(TTFont("IRANSansMedium", str(FONT_MEDIUM)))
    if "IRANSansBlack" not in registered:
        pdfmetrics.registerFont(TTFont("IRANSansBlack", str(FONT_BLACK)))


def _fa_digits(value):
    return str(value).translate(FA_TRANS)


def _shape(value):
    text = "" if value is None else str(value)

    # Remove invisible bidi control characters commonly introduced
    # by Telegram/Instagram copy-paste. Keep Persian ZWNJ intact.
    for ch in (
        "\u2066", "\u2067", "\u2068", "\u2069",
        "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
        "\u200e", "\u200f",
    ):
        text = text.replace(ch, "")

    if not text:
        return ""
    shaped_lines = []
    for line in text.splitlines() or [""]:
        shaped_lines.append(get_display(arabic_reshaper.reshape(line)))
    return "\n".join(shaped_lines)


def _clean_location(value):
    value = str(value or "").replace("#", " ").strip()
    return " ".join(value.split())


def _fmt_num(value):
    if value is None:
        return "—"
    try:
        f = float(value)
        if f.is_integer():
            return _fa_digits(int(f))
        return _fa_digits(("%.2f" % f).rstrip("0").rstrip("."))
    except Exception:
        return _fa_digits(value)


def _fmt_price_total(toman):
    if not toman:
        return "—"
    n = int(toman)
    if n >= 1_000_000_000:
        return _fa_digits(("%.2f" % (n / 1_000_000_000)).rstrip("0").rstrip(".")) + " میلیارد"
    if n >= 1_000_000:
        return _fa_digits(round(n / 1_000_000)) + " میلیون"
    return _fa_digits(f"{n:,}") + " تومان"


def _fmt_price_m2(toman, area):
    try:
        if not toman or not area or float(area) <= 0:
            return "—"
        ppm = int(round(int(toman) / float(area)))
        if ppm >= 1_000_000:
            return _fa_digits(round(ppm / 1_000_000)) + " میلیون"
        return _fa_digits(f"{ppm:,}")
    except Exception:
        return "—"


def _dedupe_parts(parts):
    seen = set()
    out = []
    for part in parts:
        part = " ".join(str(part or "").split()).strip(" -،,")
        if not part:
            continue
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(part)
    return out


def _features(row):
    parts = []
    if row["installations_text"]:
        parts.append(row["installations_text"])

    for text_value, has_value, yes_text, no_text in (
        (row["parking_text"], row["has_parking"], "پارکینگ", "بدون پارکینگ"),
        (row["elevator_text"], row["has_elevator"], "آسانسور", "بدون آسانسور"),
        (row["storage_text"], row["has_storage"], "انباری", "بدون انباری"),
    ):
        if text_value:
            parts.append(text_value)
        elif has_value is True:
            parts.append(yes_text)
        elif has_value is False:
            parts.append(no_text)

    return " - ".join(_dedupe_parts(parts)) or "—"


def _attribute_map(attrs):
    result = {}
    for item in attrs or []:
        key = item.get("key")
        if key:
            result[key] = item
    return result


def _clean_description_text(value):
    text = "" if value is None else str(value).strip()

    # Invisible bidi controls
    for ch in (
        "\u2066", "\u2067", "\u2068", "\u2069",
        "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
        "\u200e", "\u200f",
    ):
        text = text.replace(ch, "")

    # Decorative markers that may come from source Telegram posts
    for marker in ("〽️", "✅", "🔸", "🔹", "▪️", "▫️"):
        text = text.replace(marker, " ")

    text = " ".join(text.split()).strip()

    # Common spacing cleanup for Persian property descriptions
    text = text.replace("نورگیرفوق", "نورگیر فوق")

    # Remove raw parser labels from printable descriptions.
    # Useful content remains; only technical/source labels disappear.
    raw_prefixes = (
        "وضعیت",
        "موقعیت",
        "تعداد خواب",
        "خواب مستر",
    )

    changed = True
    while changed:
        changed = False
        for prefix in raw_prefixes:
            new_text = re.sub(
                rf"^\s*{re.escape(prefix)}\s*[:：]\s*",
                "",
                text,
                count=1,
            )
            if new_text != text:
                text = new_text.strip()
                changed = True

    # If a bedroom description contains master-bedroom information,
    # keep only the useful master-bedroom part because total bedrooms
    # already has its own PDF column.
    if "خواب مستر" in text:
        master = re.search(
            r"([۰-۹٠-٩0-9]+|یک|دو|سه|چهار|پنج|شش|هفت|هشت|نه|ده)\s*خواب\s*مستر",
            text,
        )
        if master:
            text = f"{master.group(1)} خواب مستر"

    # Remove redundant description label
    for prefix in (
        "توضیحات:",
        "توضیحات :",
        "توضیحات：",
    ):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()

    return text.strip(" -–—،؛|")


def _description(row):
    attrs = row["attributes"] or []
    parts = []
    seen = set()

    def norm(value):
        value = _clean_description_text(value)
        return re.sub(r"[\s‌\-–—،؛|:：]+", "", value)

    def is_structured_duplicate(value):
        value = _clean_description_text(value)
        compact = value.replace(" ", "").replace("‌", "")

        if not value:
            return True

        # These belong in dedicated columns, not description.
        if re.match(
            r"^(?:تعداد\s*واحد\s*)?(?:در\s*)?(?:هر\s*)?طبقه\s*[:：]?",
            value,
        ):
            return True

        if re.match(r"^تعداد\s*کل\s*طبقات\s*[:：]?", value):
            return True

        # Bedroom count already has a dedicated column.
        if row.get("bedrooms") not in (None, ""):
            if re.fullmatch(
                r"(?:[۰-۹٠-٩0-9]+|یک|دو|سه|چهار|پنج|شش|هفت|هشت|نه|ده)خواب",
                compact,
            ):
                return True

        # Remove only simple exact duplicates of dedicated feature columns.
        if row.get("parking_text") or row.get("has_parking") is not None:
            if compact in {
                "دارایپارکینگ",
                "بدونپارکینگ",
                "پارکینگدارد",
                "پارکینگندارد",
            }:
                return True

        if row.get("elevator_text") or row.get("has_elevator") is not None:
            if compact in {
                "دارایآسانسور",
                "بدونآسانسور",
                "آسانسوردارد",
                "آسانسورندارد",
            }:
                return True

        if row.get("storage_text") or row.get("has_storage") is not None:
            if compact in {
                "دارایانباری",
                "بدونانباری",
                "انباریدارد",
                "انبارندارد",
            }:
                return True

        # Direction already has a dedicated PDF column.
        if row.get("direction") and compact in {
            "شمالی",
            "جنوبی",
            "شرقی",
            "غربی",
        }:
            return True

        return False

    def add_segment(value):
        value = _clean_description_text(value)

        if not value or is_structured_duplicate(value):
            return

        key = norm(value)

        if not key or key in seen:
            return

        seen.add(key)
        parts.append(value)

    def add_description(value):
        value = _clean_description_text(value)

        if not value:
            return

        # Break long free text into useful independent phrases.
        chunks = re.split(r"\s*(?:[-–—|]|[،؛])\s*", value)

        for chunk in chunks:
            add_segment(chunk)

    if row["special_notes"]:
        add_description(row["special_notes"])

    skip_keys = {
        "deed_type",
        "installations",
        "parking",
        "elevator",
        "storage",
        "total_floors",
        "units_per_floor",
    }

    for item in attrs:
        key = item.get("key")

        if key in skip_keys:
            continue

        label = (item.get("label") or "").strip()
        raw_value = item.get("value") or ""
        value = _clean_description_text(raw_value)

        if not value:
            continue

        # Main description: print content only, never "توضیحات:"
        if key == "property_description":
            add_description(value)
            continue

        # Old parser pollution:
        # Sometimes the complete description was mistakenly saved
        # inside another attribute such as balcony.
        raw_text = str(raw_value)

        if (
            "توضیحات:" in raw_text
            or "توضیحات :" in raw_text
            or raw_text.count("-") >= 3
        ):
            continue

        if is_structured_duplicate(value):
            continue

        # If this value already exists inside description, don't repeat it.
        value_key = norm(value)

        if any(value_key and value_key in norm(existing) for existing in parts):
            continue

        add_segment(f"{label}: {value}" if label else value)

    return " - ".join(parts) or "—"


def fetch_active_properties(min_price_toman=None, max_price_toman=None):
    filters = [
        "p.status = 'ACTIVE'",
        "p.deleted_from_channel_at IS NULL",
        """(
            p.published_message_id IS NOT NULL
            OR EXISTS (
                SELECT 1
                FROM publish_queue q_wait
                WHERE q_wait.property_id = p.id
                  AND q_wait.queue_status IN (
                      'NEW',
                      'PARSED',
                      'READY',
                      'SCHEDULED',
                      'FAILED'
                  )
            )
        )""",
    ]
    params = []

    if min_price_toman is not None:
        filters.append("p.current_price_toman >= %s")
        params.append(int(min_price_toman))

    if max_price_toman is not None:
        filters.append("p.current_price_toman <= %s")
        params.append(int(max_price_toman))

    where_sql = "\n          AND ".join(filters)

    sql = f"""
        SELECT
            p.id,
            p.unit_code,
            p.property_type,
            p.district,
            p.address_text,
            p.area_m2,
            p.floor_text,
            p.units_per_floor,
            p.bedrooms,
            p.installations_text,
            p.deed_text,
            p.direction,
            p.build_year,
            p.current_price_toman,
            p.special_notes,
            p.has_parking,
            p.has_elevator,
            p.has_storage,
            p.parking_text,
            p.elevator_text,
            p.storage_text,
            CASE
                WHEN p.published_message_id IS NOT NULL
                    THEN 'PUBLISHED'
                ELSE (
                    SELECT q.queue_status
                    FROM publish_queue q
                    WHERE q.property_id = p.id
                      AND q.queue_status IN (
                          'NEW',
                          'PARSED',
                          'READY',
                          'SCHEDULED',
                              'FAILED'
                      )
                    ORDER BY
                        q.updated_at DESC,
                        q.id DESC
                    LIMIT 1
                )
            END AS report_status_code,
            (
                SELECT ps.source_unit_code
                FROM property_sources ps
                WHERE ps.property_id = p.id
                  AND ps.is_active = TRUE
                  AND ps.source_unit_code IS NOT NULL
                  AND BTRIM(ps.source_unit_code) <> ''
                ORDER BY
                    ps.is_primary DESC,
                    ps.id DESC
                LIMIT 1
            ) AS source_unit_code,
            (
                SELECT COALESCE(
                    jsonb_agg(
                        jsonb_build_object(
                            'key', pa.attribute_key,
                            'label', pa.attribute_label,
                            'value', pa.attribute_value,
                            'order', pa.display_order
                        )
                        ORDER BY pa.display_order, pa.id
                    ),
                    '[]'::jsonb
                )
                FROM property_attributes pa
                WHERE pa.property_id = p.id
                  AND pa.is_visible = TRUE
            ) AS attributes
        FROM properties p
        WHERE {where_sql}
        ORDER BY
            p.current_price_toman ASC NULLS LAST,
            p.id ASC
    """

    keys = [
        "id", "unit_code", "property_type", "district", "address_text",
        "area_m2", "floor_text", "units_per_floor", "bedrooms",
        "installations_text", "deed_text", "direction", "build_year",
        "current_price_toman", "special_notes", "has_parking",
        "has_elevator", "has_storage", "parking_text", "elevator_text",
        "storage_text", "report_status_code", "source_unit_code", "attributes",
    ]

    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(zip(keys, row)) for row in cur.fetchall()]


# === PDF QUEUE TRACKING CODE V1 ===
def _report_tracking_code(row):
    if row.get("report_status_code") == "PUBLISHED":
        unit_code = row.get("unit_code")
        return (
            _fa_digits(unit_code)
            if unit_code
            else "—"
        )

    return "صف " + _fa_digits(
        row["id"]
    )


def _cell(value, style):
    shaped = _shape(value)
    safe = html.escape(shaped).replace("\n", "<br/>")
    return Paragraph(safe, style)


def build_property_pdf(min_price_toman=None, max_price_toman=None, price_label=None):
    _register_fonts()
    rows = fetch_active_properties(
        min_price_toman=min_price_toman,
        max_price_toman=max_price_toman,
    )
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=5 * mm,
        leftMargin=5 * mm,
        topMargin=5 * mm,
        bottomMargin=5 * mm,
        title="لیست فایل‌های فعال و صف انتشار",
        author="RealEstate Bot",
    )

    title_style = ParagraphStyle(
        "TitleRTL",
        fontName="IRANSansBlack",
        fontSize=11,
        leading=14,
        alignment=TA_CENTER,
        spaceAfter=2 * mm,
    )
    meta_style = ParagraphStyle(
        "MetaRTL",
        fontName="IRANSansMedium",
        fontSize=6.5,
        leading=8,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#555555"),
        spaceAfter=4 * mm,
    )
    header_style = ParagraphStyle(
        "HeaderRTL",
        fontName="IRANSansBlack",
        fontSize=6.4,
        leading=8.2,
        alignment=TA_CENTER,
        textColor=colors.white,
    )
    body_style = ParagraphStyle(
        "BodyRTL",
        fontName="IRANSans",
        fontSize=6.2,
        leading=8.2,
        alignment=TA_RIGHT,
    )
    center_style = ParagraphStyle(
        "CenterRTL",
        parent=body_style,
        alignment=TA_CENTER,
    )

    now = datetime.now(TEHRAN)

    range_text = price_label or "همه قیمت‌ها"

    published_count = sum(
        1
        for row in rows
        if row.get("report_status_code") == "PUBLISHED"
    )
    queue_count = len(rows) - published_count

    story = [
        _cell("لیست فایل‌های فعال + صف انتشار", title_style),
        _cell(
            f"بازه قیمت: {range_text} - "
            f"تعداد کل: {_fa_digits(len(rows))} - "
            f"منتشرشده: {_fa_digits(published_count)} - "
            f"در صف: {_fa_digits(queue_count)} - "
            f"مرتب‌شده بر اساس قیمت کل - "
            f"زمان تهیه: {_fa_digits(now.strftime('%Y/%m/%d %H:%M'))}",
            meta_style,
        ),
        Spacer(1, 0.8 * mm),
    ]

    headers_rtl = [
        "کد واحد / صف",
        "وضعیت",
        "نوع ملک",
        "منطقه",
        "متراژ",
        "طبقه",
        "واحد در طبقه",
        "خواب",
        "نصبیات / امکانات",
        "سند",
        "جهت",
        "سال ساخت",
        "قیمت کل",
        "قیمت هر متر",
        "توضیحات",
    ]

    logical_rows = []

    for row in rows:
        attrs = _attribute_map(row["attributes"])
        deed = row["deed_text"]
        if not deed and "deed_type" in attrs:
            deed = attrs["deed_type"].get("value")

        location = _clean_location(row["address_text"] or row["district"])
        property_type = TYPE_LABELS.get(row["property_type"], row["property_type"] or "—")

        units_per_floor = row["units_per_floor"]

        if units_per_floor in (None, "") and "units_per_floor" in attrs:
            units_per_floor = attrs["units_per_floor"].get("value")

        code_display = _report_tracking_code(
            row
        )

        status_display = REPORT_STATUS_LABELS.get(
            row.get("report_status_code"),
            row.get("report_status_code") or "—",
        )

        logical_rows.append([
            code_display,
            status_display,
            property_type,
            location or "—",
            _fmt_num(row["area_m2"]),
            row["floor_text"] or "—",
            _fmt_num(units_per_floor),
            _fmt_num(row["bedrooms"]),
            _features(row),
            deed or "—",
            row["direction"] or "—",
            _fmt_num(row["build_year"]),
            _fmt_price_total(row["current_price_toman"]),
            _fmt_price_m2(row["current_price_toman"], row["area_m2"]),
            _description(row),
        ])

    header_row = [_cell(value, header_style) for value in reversed(headers_rtl)]
    data = [header_row]

    for row in logical_rows:
        reversed_row = list(reversed(row))
        rendered = []
        for idx, value in enumerate(reversed_row):
            logical_index = len(headers_rtl) - 1 - idx
            style = body_style if logical_index in {8, 14} else center_style
            rendered.append(_cell(value, style))
        data.append(rendered)

    if not logical_rows:
        data.append(
            [_cell("فایلی برای نمایش وجود ندارد.", body_style)]
            + [_cell("", body_style) for _ in range(len(headers_rtl) - 1)]
        )

    col_widths_mm = [44, 19, 22, 13, 11, 16, 34, 10, 12, 12, 12, 25, 14, 27, 16]
    col_widths = [x * mm for x in col_widths_mm]

    table = LongTable(data, colWidths=col_widths, repeatRows=1, hAlign="CENTER")

    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#273444")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8BDC4")),
        ("LEFTPADDING", (0, 0), (-1, -1), 1.2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1.2),
        ("TOPPADDING", (0, 0), (-1, -1), 2.0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.0),
    ]

    for idx in range(1, len(data)):
        if idx % 2 == 0:
            style_cmds.append(
                ("BACKGROUND", (0, idx), (-1, idx), colors.HexColor("#F4F6F8"))
            )

    table.setStyle(TableStyle(style_cmds))
    story.append(table)

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("IRANSans", 7)
        page = _shape(f"صفحه {_fa_digits(document.page)}")
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawCentredString(landscape(A4)[0] / 2, 6 * mm, page)
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)

    pdf_bytes = buffer.getvalue()
    buffer.close()

    filename = f"active_and_queue_properties_{now.strftime('%Y%m%d_%H%M')}.pdf"
    return pdf_bytes, filename, len(rows)
