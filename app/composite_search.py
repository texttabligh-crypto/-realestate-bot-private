"""Shared AND-based free-text predicates for manager search and categories."""
import re

try:
    from .property_parser import KNOWN_DISTRICTS
except ImportError:
    from property_parser import KNOWN_DISTRICTS


_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
# A small number right after a district is part of its name: «ولیعصر ۲»,
# «ولیعصر فاز ۲». On its own the number would match almost any listing.
_DISTRICT_NUMBER = re.compile(r"[\s_-]*(?:فاز[\s_-]*)?(\d{1,2})(?!\d)")
_SEPARATORS = "[[:space:]_‌-]*"


def composite_search_terms(text):
    """
    (term, kind) pairs: kind "location" for a district, "numbered" for a
    district with its number («ولیعصر 2») and "text" for any other word.
    """

    text = str(text or "").replace("ي", "ی").replace("ك", "ک").translate(_DIGITS)
    text = re.sub(r"پیش[\s_‌-]*فروش", "پیشفروش", text)
    text = text.replace("‌", " ").replace("#", "")
    # Preserve multiword district names as one location predicate.
    remaining = text
    terms = []
    for district in sorted(KNOWN_DISTRICTS, key=len, reverse=True):
        index = remaining.find(district)
        if index < 0:
            continue
        after = remaining[index + len(district):]
        number = _DISTRICT_NUMBER.match(after)
        if number:
            terms.append((f"{district} {number.group(1)}", "numbered"))
            remaining = remaining[:index] + " " + after[number.end():]
        else:
            terms.append((district, "location"))
            remaining = remaining.replace(district, " ")
    for term in remaining.split():
        if term.strip("،,"):
            terms.append((term.strip("،,"), "text"))
    return list(dict.fromkeys(terms))


def _numbered_pattern(term):
    """«ولیعصر 2» -> a regex for «ولیعصر ۲», «ولیعصر2», «#ولیعصر_۲», «ولیعصر فاز ۲», not «ولیعصر ۲۰»."""

    district, number = term.rsplit(" ", 1)
    name = _SEPARATORS.join(re.escape(part) for part in district.split())
    return f"{name}{_SEPARATORS}(فاز{_SEPARATORS})?{number}([^0-9]|$)"


def _digits_sql(expression):
    return (
        "translate(" + expression + ", 'يك۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', "
        "'یک01234567890123456789')"
    )


def _compact_sql(expression):
    for old, new in (
        ("ي", "ی"), ("ك", "ک"), ("‌", ""),
        (" ", ""), ("_", ""), ("#", ""),
    ):
        expression = "replace(" + expression + ", '" + old + "', '" + new + "')"
    return expression


def composite_text_predicates(text):
    clauses, params = [], []
    for term, kind in composite_search_terms(text):
        needle = "%" + term.replace(" ", "").replace("‌", "") + "%"
        if kind == "numbered":
            fields = ["p.district", "p.neighborhood", "p.address_text", "p.raw_text"]
            pieces = [_digits_sql("COALESCE(" + field + ", '')") + " ~ %s" for field in fields]
            pieces.append(
                "EXISTS (SELECT 1 FROM property_sources ps "
                "WHERE ps.property_id = p.id AND ps.is_active = TRUE AND " +
                _digits_sql("COALESCE(ps.source_raw_text, '')") + " ~ %s)"
            )
            clauses.append("(" + " OR ".join(pieces) + ")")
            params.extend([_numbered_pattern(term)] * (len(fields) + 1))
        elif kind == "location":
            fields = ["p.district", "p.neighborhood", "p.address_text"]
            clauses.append("(" + " OR ".join(
                _compact_sql("COALESCE(" + field + ", '')") + " ILIKE %s"
                for field in fields
            ) + ")")
            params.extend([needle] * len(fields))
        else:
            fields = ["p.unit_code", "p.raw_text", "p.floor_text",
                      "p.address_text", "p.district",
                      "p.area_m2::text", "p.current_price_billion::text"]
            pieces = [
                _compact_sql("COALESCE(" + field + ", '')") + " ILIKE %s"
                for field in fields
            ]
            pieces.append(
                "EXISTS (SELECT 1 FROM property_attributes pa "
                "WHERE pa.property_id = p.id AND (" +
                _compact_sql("COALESCE(pa.attribute_label, '')") + " ILIKE %s OR " +
                _compact_sql("COALESCE(pa.attribute_value, '')") + " ILIKE %s))"
            )
            pieces.append(
                "EXISTS (SELECT 1 FROM property_sources ps "
                "WHERE ps.property_id = p.id AND ps.is_active = TRUE AND (" +
                _compact_sql("COALESCE(ps.source_raw_text, '')") + " ILIKE %s OR " +
                _compact_sql("COALESCE(ps.source_unit_code, '')") + " ILIKE %s))"
            )
            clauses.append("(" + " OR ".join(pieces) + ")")
            params.extend([needle] * (len(fields) + 4))
    return clauses, params
