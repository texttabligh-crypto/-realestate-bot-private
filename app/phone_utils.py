import re


def normalize_iran_phone(value: str) -> str:
    """Return Iranian mobiles in locally callable 09xxxxxxxxx form."""
    translated = str(value or "").translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789",
        )
    )
    digits = re.sub(r"\D+", "", translated)

    if digits.startswith("0098") and len(digits) == 14:
        return "0" + digits[4:]

    if digits.startswith("98") and len(digits) == 12:
        return "0" + digits[2:]

    if digits.startswith("9") and len(digits) == 10:
        return "0" + digits

    return digits
