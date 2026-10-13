"""Shared AND-based free-text predicates for manager search and categories."""
import re

try:
    from .property_parser import KNOWN_DISTRICTS
except ImportError:
    from property_parser import KNOWN_DISTRICTS


def composite_search_terms(text):
    text = str(text or "").replace("ي", "ی").replace("ك", "ک")
    text = re.sub(r"پیش[\s_‌-]*فروش", "پیشفروش", text)
    text = text.replace("‌", " ").replace("#", "")
    # Preserve multiword district names as one location predicate.
    remaining = text
    terms = []
    for district in sorted(KNOWN_DISTRICTS, key=len, reverse=True):
        if district in remaining:
            terms.append((district, True))
            remaining = remaining.replace(district, " ")
    for term in remaining.split():
        if term.strip("،,"):
            terms.append((term.strip("،,"), False))
    return list(dict.fromkeys(terms))


def _compact_sql(expression):
    for old, new in (
        ("ي", "ی"), ("ك", "ک"), ("‌", ""),
        (" ", ""), ("_", ""), ("#", ""),
    ):
        expression = "replace(" + expression + ", '" + old + "', '" + new + "')"
    return expression


def composite_text_predicates(text):
    clauses, params = [], []
    for term, is_location in composite_search_terms(text):
        needle = "%" + term.replace(" ", "").replace("‌", "") + "%"
        if is_location:
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
