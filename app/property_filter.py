import re


PROPERTY_WORDS = [
    "آپارتمان",
    "واحد",
    "فروش",
    "فروشی",
    "ملک",
    "ویلا",
    "زمین",
    "خانه",
    "کلنگی",
    "مغازه",
    "تجاری",
    "اداری",
]

PROPERTY_FEATURES = [
    "متر",
    "متری",
    "خواب",
    "طبقه",
    "پارکینگ",
    "آسانسور",
    "انباری",
    "سند",
    "ساخت",
    "نوساز",
    "جنوبی",
    "شمالی",
    "واحد در طبقه",
]

PRICE_WORDS = [
    "میلیارد",
    "میلیون",
    "تومان",
    "قیمت",
]

NON_PROPERTY_WORDS = [
    "استخدام",
    "تبریک",
    "تسلیت",
    "اطلاعیه",
    "آگهی استخدام",
    "نیروی کار",
    "مشاور استخدام",
    "همکاری",
    "قرعه کشی",
    "مسابقه",
    "تولد",
]


def normalize_text(text: str) -> str:
    if not text:
        return ""

    text = text.replace("ي", "ی")
    text = text.replace("ك", "ک")
    text = text.replace("ۀ", "ه")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def contains_number(text: str) -> bool:
    return bool(
        re.search(
            r"[0-9۰-۹]",
            text
        )
    )


def count_matches(text: str, words: list[str]) -> int:
    return sum(
        1 for word in words
        if word in text
    )


def classify_property(text: str, has_media: bool = False) -> dict:
    text = normalize_text(text)

    if not text:
        return {
            "classification": "INVALID_PROPERTY",
            "score": 0,
            "reason": "متن فایل خالی است",
        }

    non_property_hits = count_matches(
        text,
        NON_PROPERTY_WORDS,
    )

    property_word_hits = count_matches(
        text,
        PROPERTY_WORDS,
    )

    feature_hits = count_matches(
        text,
        PROPERTY_FEATURES,
    )

    price_hits = count_matches(
        text,
        PRICE_WORDS,
    )

    has_number = contains_number(text)

    score = 0

    score += property_word_hits * 2
    score += feature_hits * 2
    score += price_hits * 2

    if has_number:
        score += 1

    if has_media:
        score += 1

    if non_property_hits >= 1 and feature_hits == 0:
        return {
            "classification": "NON_PROPERTY",
            "score": score,
            "reason": "پیام دارای نشانه‌های محتوای غیرملکی است",
        }

    if (
        feature_hits >= 2
        and price_hits >= 1
        and has_number
    ):
        return {
            "classification": "PROPERTY",
            "score": score,
            "reason": "مشخصات و قیمت ملک تشخیص داده شد",
        }

    if (
        property_word_hits >= 1
        and feature_hits >= 2
        and has_number
    ):
        return {
            "classification": "PROPERTY",
            "score": score,
            "reason": "نشانه‌های کافی برای فایل ملک وجود دارد",
        }

    if score >= 7:
        return {
            "classification": "PROPERTY",
            "score": score,
            "reason": "امتیاز تشخیص فایل ملکی کافی است",
        }

    if property_word_hits >= 1 or feature_hits >= 1:
        return {
            "classification": "INVALID_PROPERTY",
            "score": score,
            "reason": "پیام شبیه فایل ملک است ولی اطلاعات کافی ندارد",
        }

    return {
        "classification": "NON_PROPERTY",
        "score": score,
        "reason": "نشانه کافی از فایل ملکی پیدا نشد",
    }
