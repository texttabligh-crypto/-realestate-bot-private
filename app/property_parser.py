import re
import hashlib
from decimal import Decimal, InvalidOperation

PERSIAN_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹",
    "0123456789"
)

ARABIC_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩",
    "0123456789"
)

KNOWN_DISTRICTS = [
    "باغمیشه",
    "ولیعصر جنوبی",
    "ولیعصر شمالی",
    "ولیعصر",
    "رشدیه",
    "الهیه",
    "مرزداران",
    "زعفرانیه",
    "فرشته جنوبی",
    "فرشته شمالی",
    "فرشته",
    "نیاوران",
    "ائل گلی",
    "شاهگلی",
    "آبرسان",
    "منظریه",
    "ولیپور",
    "سهند",
    "پرواز",
    "میرداماد",
    "گلشهر",
]


# === COMPOUND DISTRICT FIDELITY V2 ===
# Canonicalize common source-channel spellings before district/direction parsing.
DISTRICT_ALIASES = {
    "ولیعصرجنوبی": "ولیعصر جنوبی",
    "ولیعصر جنوبی": "ولیعصر جنوبی",
    "ولیعصرشمالی": "ولیعصر شمالی",
    "ولیعصر شمالی": "ولیعصر شمالی",
    "فرشتهجنوبی": "فرشته جنوبی",
    "فرشته جنوبی": "فرشته جنوبی",
    "فرشتهشمالی": "فرشته شمالی",
    "فرشته شمالی": "فرشته شمالی",
    "ائلگلی": "ائل گلی",
    "ائل گلی": "ائل گلی",
    "شاهگلی": "شاهگلی",
}

def _canonicalize_location_text(value: str) -> str:
    value = str(value or "")
    # Longest aliases first to avoid plain ولیعصر winning over compound names.
    for alias, canonical in sorted(
        DISTRICT_ALIASES.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if alias in value:
            value = value.replace(alias, canonical)
    return re.sub(r"\s+", " ", value).strip()


def normalize_text(text: str) -> str:
    if not text:
        return ""

    text = text.translate(PERSIAN_DIGITS)
    text = text.translate(ARABIC_DIGITS)
    text = text.replace("ي", "ی")
    text = text.replace("ك", "ک")
    text = text.replace("ۀ", "ه")
    text = text.replace("ـ", "")
    text = text.replace("٫", ".")
    text = text.replace("٬", ",")
    text = text.replace("،", ",")

    # Telegram/copy-paste text may contain bidi and zero-width
    # characters between words. Treat them as ordinary spaces so
    # explicit phrases such as «بدون آسانسور» keep their meaning.
    text = re.sub(
        r"[\u200b\u200c\u200d\u200e\u200f"
        r"\u202a-\u202e\u2066-\u2069\ufeff]",
        " ",
        text,
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)

    return text.strip()


# Some source channels pin one «catalog» post listing several units, each
# starting with its own «کد فایل» line, and keep editing it. Parsing it as
# one listing mixes every unit's parking, elevator and detail lines together.
_LISTING_CODE_LINE = re.compile(
    r"(?m)^[^\w\n]{0,8}کد\s*(?:فایل|واحد|ملک|آگهی)\s*[:：\-]?\s*(\d+)"
)


def split_listing_blocks(text: str):
    """Return [(code, text)] per unit of a multi-listing post, else [].

    Each block keeps the shared header (hashtags/title before the first
    code), so a unit parses exactly as if it had been posted on its own.
    """
    text = normalize_text(text)
    matches = list(_LISTING_CODE_LINE.finditer(text))
    if len({match.group(1) for match in matches}) < 2:
        return []

    header = text[:matches[0].start()]
    blocks = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append((match.group(1), header + text[match.start():end].strip()))
    return blocks


def primary_listing_text(text: str) -> str:
    """The first unit of a multi-listing post; any other text unchanged."""
    blocks = split_listing_blocks(text)
    return blocks[0][1] if blocks else text


def first_number(patterns, text, cast=float):
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)

        if match:
            value = match.group(1)

            try:
                return cast(value)
            except (ValueError, InvalidOperation):
                continue

    return None


# «۱۴۳/۵» uses a slash as the decimal separator.
_AREA_NUMBER = r"(\d+(?:[./]\d+)?)"
_LOCATION_LINE = re.compile(r"📍|خیابان|کوچه|بلوار|میدان")


def _area_value(value):
    return float(value.replace("/", "."))


def parse_area(text: str):
    # The first «N متر/متری» is the size, except that on a location line
    # «N متری» names a street («📍 ائل‌گلی | ۳۵ متری سینا»).
    for line in (text or "").splitlines():
        is_location = _LOCATION_LINE.search(line)
        for match in re.finditer(_AREA_NUMBER + r"\s*متر(ی)?", line):
            if is_location and match.group(2):
                continue
            return _area_value(match.group(1))

    match = re.search(r"متراژ\s*[:：\-]?\s*" + _AREA_NUMBER, text or "")
    if match:
        return _area_value(match.group(1))

    return None



def parse_bedrooms(text: str):
    value = first_number(
        [
            r"(\d+)\s*خواب",
            r"خواب\s*[:：\-]?\s*(\d+)",
        ],
        text,
        int,
    )

    if value is not None:
        return value

    word_numbers = {
        "تک": 1,
        "یه": 1,
        "یک": 1,
        "دو": 2,
        "سه": 3,
        "چهار": 4,
        "پنج": 5,
        "شش": 6,
    }

    matches = re.findall(
        r"(تک|یه|یک|دو|سه|چهار|پنج|شش)\s*خواب",
        text,
    )

    if matches:
        # The first bedroom count is the total number of bedrooms.
        #
        # Examples:
        # "دو خواب" -> 2
        # "دو خواب یک خواب مستر" -> 2
        # "سه خواب دو خواب مستر" -> 3
        # "یه خواب یک خواب مستر" -> 1
        return word_numbers[matches[0]]

    return None



def parse_floor(text: str):
    """
    Extract the actual unit floor, while avoiding phrases such as:
    - هر طبقه 2 واحد
    - تعداد واحد در طبقه
    - ساختمان 5 طبقه
    - دارای 3 طبقه قابل سکونت

    Supported examples:
    - طبقه: 4
    - طبقه 4 از 8
    - طبقه چهارم
    - طبقه پانزدهم از 16
    - طبقه ی 9
    - طبقه 1 و 3 از 5
    - طبقه 1و3و4 از 5
    - طبقه دو و شش
    """

    if not text:
        return None

    text = str(text)

    # Normalize Persian / Arabic digits and letters.
    text = text.translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789",
        )
    )

    text = text.replace("ي", "ی")
    text = text.replace("ك", "ک")
    text = text.replace("ۀ", "ه")
    text = text.replace("ـ", "")

    # Remove bidi / zero-width characters which often exist
    # around Telegram emojis and forwarded text.
    text = re.sub(
        r"[\u200b\u200c\u200d\u200e\u200f"
        r"\u202a-\u202e\u2066-\u2069\ufeff]",
        "",
        text,
    )

    floor_words = [
        "نوزدهم",
        "هجدهم",
        "هفدهم",
        "شانزدهم",
        "پانزدهم",
        "چهاردهم",
        "سیزدهم",
        "دوازدهم",
        "یازدهم",
        "بیستم",
        "چهارم",
        "پنجم",
        "ششم",
        "هفتم",
        "هشتم",
        "نهم",
        "دهم",
        "سوم",
        "دوم",
        "اول",
        "همکف",
        "زیرزمین",
        "نوزده",
        "هجده",
        "هفده",
        "شانزده",
        "پانزده",
        "چهارده",
        "سیزده",
        "دوازده",
        "یازده",
        "بیست",
        "ده",
        "نه",
        "هشت",
        "هفت",
        "شش",
        "پنج",
        "چهار",
        "سه",
        "دو",
        "یک",
    ]

    word_pattern = (
        "(?:"
        + "|".join(
            re.escape(word)
            for word in floor_words
        )
        + ")"
    )

    atom = rf"(?:\d+|{word_pattern})"

    value_pattern = (
        rf"{atom}"
        rf"(?:\s*و\s*{atom})*"
        rf"(?:\s*از\s*{atom})?"
    )

    def normalize_result(value):
        value = str(value).strip()

        # Normalize compact numeric multi-floor values:
        # 1و3و4 -> 1 و 3 و 4
        #
        # Do NOT touch the letter "و" inside words such as:
        # دوم / سوم
        value = re.sub(
            r"(?<=\d)\s*و\s*(?=\d)",
            " و ",
            value,
        )

        value = re.sub(
            r"\s+از\s+",
            " از ",
            value,
        )

        value = re.sub(
            r"\s+",
            " ",
            value,
        ).strip()

        return value[:100]

    # -------------------------------------------------
    # Preferred mode:
    # Explicit floor line beginning with "طبقه".
    #
    # Leading emojis / decorations are ignored.
    # -------------------------------------------------
    for raw_line in text.splitlines():
        line = raw_line.strip()

        # Remove arbitrary decorative characters from
        # the beginning, but keep Persian/Latin letters
        # and digits.
        line = re.sub(
            r"^[^0-9A-Za-zآ-ی]+",
            "",
            line,
        ).strip()

        match = re.match(
            rf"^طبقه"
            rf"(?:\s*ی)?"
            rf"\s*[:：\-]?\s*"
            rf"(?P<value>{value_pattern})",
            line,
            flags=re.IGNORECASE,
        )

        if match:
            return normalize_result(
                match.group("value")
            )

    # -------------------------------------------------
    # Safe inline fallback:
    # "واحد ... در طبقه سوم"
    #
    # Does NOT match vague expressions like:
    # "در طبقه میانی".
    # -------------------------------------------------
    for raw_line in text.splitlines():
        line = raw_line.strip()

        # These phrases describe building structure or
        # units-per-floor, not the current unit's floor.
        if re.search(
            r"(?:تعداد\s+)?واحد\s+در\s+طبقه",
            line,
            flags=re.IGNORECASE,
        ):
            continue

        match = re.search(
            rf"\bدر\s+طبقه"
            rf"(?:\s*ی)?"
            rf"\s*[:：\-]?\s*"
            rf"(?P<value>{value_pattern})",
            line,
            flags=re.IGNORECASE,
        )

        if match:
            return normalize_result(
                match.group("value")
            )

    return None



def parse_build_year(text: str):
    value = first_number(
        [
            r"سال\s*ساخت\s*[:：\-]?\s*(\d{4})",
            r"ساخت\s*[:：\-]?\s*(\d{4})",
            r"پایان\s*کار\s*(?:سال)?\s*(\d{4})",
            r"پایانکار\s*(?:سال)?\s*(\d{4})",
            r"پایان\s*کار\s*سال\s*(\d{4})",
            r"پایانکار\s*سال\s*(\d{4})",
            r"(\d{4})\s*پایان\s*کار",
        ],
        text,
        int,
    )

    if value and 1300 <= value <= 1500:
        return value

    return None


def parse_direction(text: str):
    """
    Extract a real property orientation without mistaking compound
    neighborhood names such as «ولیعصر جنوبی» for unit direction.
    """
    cleaned = normalize_text(text or "")

    # Remove location aliases/canonical compound district names before
    # looking for orientation words. If the source separately says
    # «جنوبی و نبش», that second occurrence remains and is detected.
    for alias, canonical in sorted(
        DISTRICT_ALIASES.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        cleaned = cleaned.replace(alias, " ")
        cleaned = cleaned.replace(canonical, " ")

    directions = [
        "شمالی-جنوبی",
        "شمالی جنوبی",
        "جنوبی-شمالی",
        "جنوبی شمالی",
        "جنوبی",
        "شمالی",
        "شرقی",
        "غربی",
    ]

    for direction in directions:
        if direction in cleaned:
            return direction

    return None


def parse_boolean_feature(text: str, feature: str):
    text = normalize_text(text or "")
    feature_pattern = re.escape(feature)
    separator = r"[\s:：\-–—|,؛]*"

    # Explicit negative wording always wins over a bare feature name.
    # This covers formal, colloquial and punctuated Telegram formats.
    negative_patterns = [
        rf"بدون(?:\s+|{separator}){feature_pattern}",
        rf"فاقد(?:\s+|{separator}){feature_pattern}",
        rf"{feature_pattern}{separator}(?:ندارد|نداره|نیست)",
        rf"{feature_pattern}{separator}نمی(?:\s*)باشد",
        rf"{feature_pattern}{separator}(?:❌|✖️?|⛔)",
    ]

    for pattern in negative_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return False

    positive_patterns = [
        rf"دارای(?:\s+|{separator}){feature_pattern}",
        rf"{feature_pattern}{separator}(?:دارد|داره|هست)",
        rf"{feature_pattern}{separator}(?:✅|✔️?)",
        rf"{feature_pattern}",
    ]

    for pattern in positive_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True

    return None



# === OFFICE ADDRESS EXCLUSION V1 ===
def _looks_like_office_location(value: str) -> bool:
    value = normalize_text(value or "").strip()

    if not value:
        return False

    return bool(
        re.match(
            r"^(?:آدرس\s*)?"
            r"دفتر"
            r"(?:\s*(?:فروش|املاک))?"
            r"\s*[:：\-]",
            value,
            flags=re.IGNORECASE,
        )
    )


def _extract_pinned_location(text: str):
    """
    Extract a real property-location line beginning with 📍
    while rejecting office/contact/source information.
    """
    blocked_markers = (
        "آدرس دفتر",
        "مشاور",
        "کارشناس",
        "شماره تماس",
        "تماس",
        "آیدی",
        "تلگرام",
        "اینستاگرام",
    )

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()

        pin_index = line.find("📍")

        if pin_index < 0:
            continue

        value = line[pin_index + 1:].strip()

        # A location marker may appear in the middle of a long
        # property-details line. Stop before the next field/emoji.
        value = re.split(
            r"\s*(?:✨️?|✨|📞|📱|📢|👨‍💼|🏠|────)\s*",
            value,
            maxsplit=1,
        )[0].strip()

        # Remove invisible directional / zero-width characters
        # that sometimes appear at the beginning of copied
        # Telegram location lines.
        value = value.lstrip(
            "\u200b\u200c\u200d\u200e\u200f\ufeff"
        ).strip()

        if not value:
            continue

        if _looks_like_office_location(value):
            continue

        if any(
            marker in value
            for marker in blocked_markers
        ):
            continue

        if re.search(
            r"(?:09\d{9}|@\S+)",
            value,
        ):
            continue

        value = re.sub(
            r"^(?:لوکیشن|موقعیت|منطقه(?:\s*واحد)?|آدرس)"
            r"\s*[:：\-]?\s*",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()

        if value:
            return _canonicalize_location_text(value)[:255]

    return None


def _extract_contextual_known_district(text: str):
    """
    Find a known district from a property-description line
    when the source does not provide an explicit 📍 location.

    Advertising/contact/source lines are ignored.
    """

    blocked_markers = (
        "مشاور",
        "کارشناس",
        "املاک",
        "شماره تماس",
        "تماس",
        "آیدی",
        "تلگرام",
        "اینستاگرام",
        "سایت",
        "http",
        "www.",
        "@",
        "همراه شما",
        "بازدید",
    )

    property_markers = (
        "واقع در",
        "فروش آپارتمان",
        "فروش واحد",
        "فروش ملک",
        "آپارتمان",
        "واحد",
        "ملک",
    )

    districts = sorted(
        KNOWN_DISTRICTS,
        key=len,
        reverse=True,
    )

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()

        if not line:
            continue

        if any(
            marker in line
            for marker in blocked_markers
        ):
            continue

        if not any(
            marker in line
            for marker in property_markers
        ):
            continue

        canonical_line = _canonicalize_location_text(line)

        for alias, canonical in sorted(
            DISTRICT_ALIASES.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if alias in line or canonical in canonical_line:
                return canonical

        for district in districts:
            if district in canonical_line:
                return district

        # Generic fallback for phrases such as:
        # "یک باب حیاط واقع در بهترین نقطه آخر عباسی"
        contextual_match = re.search(
            r"واقع\s+در"
            r"(?:\s+بهترین\s+نقطه[‌\s]*)?"
            r"([^\n\r،,|؛;]{2,80})",
            line,
        )

        if contextual_match:
            value = contextual_match.group(1).strip()

            # Remove common trailing property-description words.
            value = re.split(
                r"\s+(?:"
                r"مجتمع"
                r"|برج"
                r"|طبقه"
                r"|متراژ"
                r"|\d+(?:\.\d+)?\s*متر"
                r")",
                value,
                maxsplit=1,
            )[0].strip()

            if value:
                return value[:255]

    return None


def _extract_headline_location(text: str):
    """Return the complete location written in the property headline."""
    match = re.search(
        # Source headings commonly start with emoji, including variation
        # selectors, and wrap the address onto the following line.
        r"(?:^|\n)[^\w\n]*"
        r"(?:پیش[‌ _-]*فروش|فروش)\s+"
        r"(?:آپارتمان|واحد|ملک)\s*"
        r"(?:\||[-–—:：])\s*"
        r"([^\n\r|،,؛;]{2,80})",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    value = _canonicalize_location_text(
        match.group(1).strip()
    )
    value = re.split(
        r"\s+(?:"
        r"\d+(?:\.\d+)?\s*متر(?:ی)?"
        r"|متراژ|طبقه|قیمت|\d+\s*خواب"
        r"|کد\s*(?:واحد|فایل|ملک|آگهی)"
        r")",
        value,
        maxsplit=1,
    )[0].strip()

    return value[:255] if value else None


def parse_address_text(text: str):
    """
    Prefer the most precise compatible source location.

    A short pin or explicit location such as «ولیعصر» must not hide a
    more complete property headline such as «ولیعصر لیدای جدید».
    """

    pinned_location = _extract_pinned_location(text)
    explicit_location = None

    location_match = re.search(
        r"(?:^|\n)\s*(?:📍\s*)?(?:لوکیشن|منطقه|آدرس)\s*[:：\-]?\s*([^\n\r]+)",
        text,
    )

    if not location_match:
        location_match = re.search(
            r"(?:^|\n)\s*(?:📍\s*)?موقعیت\s*[:：\-]?\s*([^\n\r]+)",
            text,
        )

    if location_match:
        value = _canonicalize_location_text(
            location_match.group(1).strip()
        )

        if not _looks_like_office_location(value):
            parts = [
                part.strip()
                for part in value.split("|")
            ]
            explicit_location = " | ".join(
                part
                for part in parts
                if part
            ) or None

    headline_location = _extract_headline_location(text)

    # Prefer the headline only when it is a strict extension of a shorter
    # explicit/pinned location. This preserves source fidelity without
    # allowing an unrelated headline to override an exact address.
    base_location = pinned_location or explicit_location
    if (
        headline_location
        and base_location
        and len(headline_location) > len(base_location)
        and base_location in headline_location
    ):
        return headline_location

    if pinned_location:
        return pinned_location

    if explicit_location:
        return explicit_location

    if headline_location:
        return headline_location

    contextual_district = (
        _extract_contextual_known_district(text)
    )

    if contextual_district:
        return contextual_district

    # Property headline fallback.
    # Example:
    #   فروش آپارتمان باغمیشه
    # should produce:
    #   باغمیشه
    headline_match = re.search(
        r"(?:^|\n)\s*(?:فروش\s+)?"
        r"(?:آپارتمان|واحد|ملک)\s+"
        r"([^\n\r|،]{2,60})",
        text,
    )

    if headline_match:
        value = headline_match.group(1).strip()

        # Remove unit details if they continue on the same line.
        value = re.split(
            r"\s+(?:"
            r"\d+(?:\.\d+)?\s*متر(?:ی)?"
            r"|متراژ"
            r"|طبقه"
            r"|قیمت"
            r"|\d+\s*خواب"
            r")",
            value,
            maxsplit=1,
        )[0].strip()

        # A line beginning with dimensions describes the unit,
        # not the location.
        if (
            value
            and not re.match(
                r"^\d+(?:\.\d+)?\s*متر(?:ی)?(?:\s|$)",
                value,
            )
        ):
            return _canonicalize_location_text(value)[:255]

    skip_words = (
        "کد واحد",
        "کد فایل",
        "کد آگهی",
        "متراژ",
        "متر",
        "طبقه",
        "خواب",
        "سال ساخت",
        "پایانکار",
        "پایان کار",
        "پارکینگ",
        "آسانسور",
        "انباری",
        "قیمت",
        "بالکن",
        "تراس",
        "نصبیات",
        "لابی",
        "کلوزت",
        "دوربین",
        "نما",
        "بدون",
        "سند",
        "مشاور",
        "کارشناس",
        "تماس",
        "شماره",
        "آیدی",
        "تلگرام",
        "اینستاگرام",
        "آدرس دفتر",
    )

    for raw_line in text.splitlines():
        line = raw_line.strip(
            " \t-•🔹🔸♦️♦▪️▪〽️"
        )

        if not line:
            continue

        if not re.search(
            r"[A-Za-zآ-ی]",
            line,
        ):
            continue

        if len(line) < 2 or len(line) > 60:
            continue

        if any(
            word in line
            for word in skip_words
        ):
            continue

        if re.search(r"\d", line):
            continue

        return line[:255]

    return None



def parse_district(text: str):
    pinned_location = _extract_pinned_location(text)

    if pinned_location:
        pinned_location = _canonicalize_location_text(
            pinned_location
        )
        return (
            re.split(
                r"[|,،;؛]",
                pinned_location,
                maxsplit=1,
            )[0]
            .strip()[:255]
        )

    location_match = re.search(
        r"(?:^|\n)\s*(?:📍\s*)?(?:لوکیشن|منطقه|آدرس)\s*[:：\-]?\s*([^\n\r]+)",
        text,
    )

    if not location_match:
        location_match = re.search(
            r"(?:^|\n)\s*(?:📍\s*)?موقعیت\s*[:：\-]?\s*([^\n\r]+)",
            text,
        )

    if location_match:
        location_line = _canonicalize_location_text(
            location_match
            .group(1)
            .strip()
        )

        if _looks_like_office_location(
            location_line
        ):
            location_line = ""

        district = (
            re.split(
                r"[|,،;؛]",
                location_line,
                maxsplit=1,
            )[0]
            .strip()
        )

        if district:
            return district[:255]

    contextual_district = (
        _extract_contextual_known_district(text)
    )

    if contextual_district:
        return contextual_district

    fallback = re.search(
        r"(?:فروش\s+)?(?:آپارتمان|واحد|ملک)\s+([^\n\r|،]{2,40})",
        text,
    )

    if fallback:
        value = fallback.group(1).strip()

        # "واحد 131 متری 2 خواب..." describes the unit,
        # not the property's district.
        if re.match(
            r"^\d+(?:\.\d+)?\s*متر(?:ی)?(?:\s|$)",
            value,
        ):
            value = ""
        else:
            value = re.split(
                r"\s+(?:\d+\s*متر(?:ی)?|متراژ|طبقه|قیمت|خواب)",
                value,
                maxsplit=1,
            )[0].strip()

        if value:
            return value[:255]

    address = parse_address_text(text)

    if address:
        return (
            re.split(
                r"[|,،;؛]",
                address,
                maxsplit=1,
            )[0]
            .strip()[:255]
        )

    return None


def parse_price_billion(text: str):
    # 1) Examples:
    #    5 میلیارد و 900 میلیون
    combo = re.search(
        r"(\d+)\s*میلیارد\s*(?:و\s*)?(\d+)\s*میلیون",
        text,
    )

    if combo:
        billions = Decimal(combo.group(1))
        millions = Decimal(combo.group(2))
        return billions + (millions / Decimal("1000"))

    # Source channels sometimes omit the word "million" and write:
    #   5 billion 600 toman
    # In real-estate captions this means 5.600 billion, not 5 billion
    # plus 600 toman. Keep the required "toman" suffix so unrelated
    # measurements are not interpreted as a price fraction.
    colloquial = re.search(
        r"(?<![\d/\.,])"
        r"(\d+)\s*میلیارد\s*(?:و\s*)?"
        r"(\d{1,3})\s*تومان"
        r"(?!\s*(?:متر|متری))",
        text,
        flags=re.IGNORECASE,
    )

    if colloquial:
        billions = Decimal(colloquial.group(1))
        fraction_text = colloquial.group(2)
        millions = Decimal(fraction_text)
        return billions + (millions / Decimal("1000"))

    # 2) Fractional billion values written with separators.
    #    Examples:
    #    15/750 میلیارد  -> 15.750
    #    15.750 میلیارد -> 15.750
    #    15,750 میلیارد -> 15.750
    #    15/75 میلیارد  -> 15.75
    separated = re.search(
        r"(?<![\d/\.,])"
        r"(\d+)\s*[/\.,]\s*(\d{1,3})"
        r"\s*میلیارد",
        text,
        flags=re.IGNORECASE,
    )

    if separated:
        whole = Decimal(separated.group(1))
        fraction_text = separated.group(2)

        fraction = (
            Decimal(fraction_text)
            / (Decimal("10") ** len(fraction_text))
        )

        return whole + fraction

    # 3) Plain integer billion values.
    #    Example:
    #    قیمت کل: 21 میلیارد
    #
    # Negative lookbehind prevents a suffix such as
    # "750 میلیارد" inside "15/750 میلیارد"
    # from being interpreted as a standalone price.
    value = first_number(
        [
            r"(?:قیمت\s*کل|قیمت|مبلغ)"
            r"\s*[:：\-]?\s*"
            r"(?<![\d/\.,])(\d+)\s*میلیارد",

            r"(?<![\d/\.,])(\d+)\s*میلیارد",
        ],
        text,
        Decimal,
    )

    if value is not None:
        return value

    # 4) Raw toman values:
    #    قیمت کل: 5.900.000.000
    #    قیمت کل: 5,900,000,000
    #    قیمت کل: ۵٬۹۰۰٬۰۰۰٬۰۰۰ تومان
    raw_match = re.search(
        r"(?:قیمت\s*کل|مبلغ\s*کل|مبلغ)"
        r"\s*[:：\-]?\s*"
        r"([0-9۰-۹٠-٩][0-9۰-۹٠-٩\.,٬،\s]{6,})"
        r"(?:\s*تومان)?",
        text,
    )

    if raw_match:
        raw_value = raw_match.group(1)

        raw_value = raw_value.translate(
            str.maketrans(
                "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                "01234567890123456789",
            )
        )

        digits = re.sub(
            r"[^0-9]",
            "",
            raw_value,
        )

        if digits:
            toman = Decimal(digits)

            # Prevent small unrelated numbers from
            # being interpreted as a property price.
            if toman >= Decimal("100000000"):
                return toman / Decimal("1000000000")

    return None

def billion_to_toman(price_billion):
    if price_billion is None:
        return None

    return int(
        price_billion * Decimal("1000000000")
    )


def make_price_hashtag(price_billion):
    if price_billion is None:
        return None

    lower = int(price_billion)
    upper = lower + 1

    return f"#آپارتمان_{lower}_{upper}"


def parse_property(text: str) -> dict:
    text = normalize_text(primary_listing_text(text))

    price_billion = parse_price_billion(text)

    return {
        "area_m2": parse_area(text),
        "bedrooms": parse_bedrooms(text),
        "floor_text": parse_floor(text),
        "build_year": parse_build_year(text),
        "district": parse_district(text),
        "address_text": parse_address_text(text),
        "has_parking": parse_boolean_feature(text, "پارکینگ"),
        "has_elevator": parse_boolean_feature(text, "آسانسور"),
        "has_storage": parse_boolean_feature(text, "انباری"),
        "direction": parse_direction(text),
        "current_price_billion": (
            float(price_billion)
            if price_billion is not None
            else None
        ),
        "current_price_toman": billion_to_toman(
            price_billion
        ),
        "price_hashtag": make_price_hashtag(
            price_billion
        ),
    }


# Messenger / social labels a source uses to advertise its own channel.
PROMOTIONAL_LABELS = {
    "بله", "ایتا", "روبیکا", "سروش", "واتساپ", "واتس اپ", "واتس‌اپ",
    "اینستاگرام", "اینستا", "تلگرام", "آیدی", "آیدی تلگرام", "لینک",
    "پیج", "سایت", "وب سایت", "وبسایت", "کانال", "کانال تلگرام", "گروه",
}
PROMOTIONAL_LINK_PATTERN = (
    r"https?://|www\.|t\.me/|ble\.ir|eitaa\.com|rubika\.ir|wa\.me|instagram\.com"
    r"|@[A-Za-z0-9_]{4,}"
)
_PROMOTIONAL_LINK = re.compile(PROMOTIONAL_LINK_PATTERN, re.IGNORECASE)


def is_promotional_attribute(label, value):
    label = re.sub(r"^[^0-9A-Za-zآ-ی]+", "", str(label or "")).strip()
    return (
        label in PROMOTIONAL_LABELS
        or bool(_PROMOTIONAL_LINK.search(f"{label} {value or ''}"))
    )


def parse_dynamic_attributes(text: str):
    text = primary_listing_text(text or "")
    attributes = []

    normalized_text = normalize_text(
        text or ""
    )
    # Some channels bullet lines with large diamonds; read them like 🔹.
    normalized_text = re.sub(
        r"(?m)^[ \t]*[🔷🔶][ \t]*", "🔹 ", normalized_text
    )

    deed_match = re.search(
        r"(?mi)^[ \t]*"
        r"(?:✨️?|〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾|🌱|📄)?[ \t]*"
        r"(?:نوع[ \t]+)?سند[ \t]*[:：\-]?[ \t]*"
        r"([^\n\r]+)$",
        normalized_text,
    )

    deed_value = None

    if deed_match:
        value = deed_match.group(1).strip()
        value = re.split(
            r"\s*(?:🔹|🔸|♦️|♦|▪️|▪|•|◾|[؛;])\s*",
            value,
            maxsplit=1,
        )[0].strip()
        if value:
            deed_value = value

    # Preserve the exact legal form even when it appears inline,
    # e.g. «نصبیات کامل؛ سند قراردادی» or «سند قول نامه‌ای».
    if not deed_value:
        inline_deed_match = re.search(
            r"(?mi)(?:نوع[ \t]+)?سند[ \t]*[:：\-]?[ \t]*"
            r"(تک[ \t]*برگ"
            r"|شش[ \t]*دانگ"
            r"|ششدانگ"
            r"|قراردادی"
            r"|قول[ \t]*نامه(?:[ \t]*ای)?"
            r"|وکالتی"
            r"|تعاونی"
            r"|مشاع"
            r"|عرصه(?:[ \t]*و[ \t]*اعیان)?"
            r"|در[ \t]*دست[ \t]*اقدام"
            r"|دردست[ \t]*اقدام"
            r"|فاقد[ \t]*سند"
            r"|پایان[ \t]*کار"
            r"|پایانکار)",
            normalized_text,
        )
        if inline_deed_match:
            deed_value = inline_deed_match.group(1).strip()

    if deed_value:
        deed_value = re.sub(r"[ \t]+", " ", deed_value).strip()
        deed_value = re.sub(r"شش[ \t]*دانگ", "ششدانگ", deed_value)
        deed_value = re.sub(
            r"(?:در[ \t]*دست|دردست)[ \t]*اقدام",
            "در دست اقدام",
            deed_value,
        )
        deed_value = re.sub(r"تک[ \t]*برگ", "تک برگ", deed_value)
        deed_value = re.sub(r"قول[ \t]*نامه(?:[ \t]*ای)?", "قولنامه‌ای", deed_value)
        deed_value = re.sub(r"پایان[ \t]*کار", "پایانکار", deed_value)

        attributes.append(
            {
                "attribute_key": "deed_type",
                "attribute_label": "نوع سند",
                "attribute_value": deed_value[:100],
                "display_order": 20,
            }
        )

    status_match = re.search(
        r"(?mi)^[ \t]*"
        r"(?:✨️?|〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾|🌱)?[ \t]*"
        r"وضعیت[ \t]*[:：\-]?[ \t]*"
        r"([^\n\r]+)$",
        normalized_text,
    )

    condition_value = None

    if status_match:
        value = status_match.group(1).strip()

        if value:
            condition_value = value

    # Some source formats do not have a dedicated "وضعیت:" line.
    # Preserve the explicit "کلید نخورده / کلیدنخورده" condition
    # wherever it appears in the property text.
    if not condition_value:
        if re.search(
            r"(?mi)\bکلید[ \t]*نخورده\b",
            normalized_text,
        ):
            condition_value = "کلید نخورده"

        elif re.search(
            r"(?mi)\bکلیدنخورده\b",
            normalized_text,
        ):
            condition_value = "کلید نخورده"

    if not condition_value:
        if re.search(
            r"(?mi)(?:^|\n)\s*"
            r"(?:✨️?|〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾|🌱)?\s*"
            r"صفر\s*تا\s*صد\s*بازسازی[‌ \t]*شده\s*$",
            normalized_text,
        ):
            condition_value = "صفر تا صد بازسازی شده"

        elif re.search(
            r"(?mi)(?:^|\n)\s*"
            r"(?:✨️?|〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾|🌱)?\s*"
            r"(?:کاملا|کاملاً)\s*بازسازی[‌ \t]*شده\s*$",
            normalized_text,
        ):
            condition_value = "کاملاً بازسازی شده"

        elif re.search(
            r"(?mi)(?:^|\n)\s*"
            r"(?:✨️?|〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾|🌱)?\s*"
            r"بازسازی\s*کامل\s*$",
            normalized_text,
        ):
            condition_value = "بازسازی کامل"

    if condition_value:
        attributes.append(
            {
                "attribute_key": "property_condition",
                "attribute_label": "وضعیت",
                "attribute_value": condition_value[:150],
                "display_order": 29,
            }
        )

    installations_match = re.search(
        r"(?mi)^[ \t]*"
        r"(?:✨️?|〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾|🌱)?[ \t]*"
        r"نصبیات[ \t]*[:：\-]?[ \t]*([^\n\r]*)$",
        normalized_text,
    )

    installations_value = None

    if installations_match:
        value = installations_match.group(1).strip()

        value = re.split(
            r"🔹|♦️|▪|•",
            value,
            maxsplit=1,
        )[0].strip()

        # Remove unrelated standard fields that sometimes
        # appear after the installations value on the same line.
        parts = [
            part.strip()
            for part in re.split(r"[؛;]", value)
            if part.strip()
        ]

        useful_parts = []

        for part in parts:
            if re.search(
                r"(?:^|\s)سند(?:\s|$)"
                r"|کلید[ \t]*نخورده"
                r"|کلیدنخورده",
                part,
            ):
                continue

            useful_parts.append(part)

        value = " ؛ ".join(useful_parts).strip()

        if re.match(
            r"^بدون[ \t]+نصبیات\b",
            value,
        ):
            value = "ندارد"

        elif re.match(
            r"^فول[ \t]+نصبیات\b",
            value,
        ):
            value = "کامل"

        if value:
            installations_value = value

    if not installations_value:
        if re.search(
            r"(?mi)^.*\bبدون[ \t]+نصبیات\b.*$",
            normalized_text,
        ):
            installations_value = "ندارد"

        elif re.search(
            r"(?mi)^.*\bفول[ \t]+نصبیات\b.*$",
            normalized_text,
        ):
            installations_value = "کامل"

    if installations_value:
        attributes.append(
            {
                "attribute_key": "installations",
                "attribute_label": "نصبیات",
                "attribute_value": installations_value[:150],
                "display_order": 30,
            }
        )

    # Common unlabelled source-channel phrases that carry real property data.
    # They are kept structured, so marketing/contact text is still excluded.
    if re.search(
        r"(?mi)(?:^|\n)\s*"
        r"(?:✨️?|〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾|🌱)?\s*"
        r"ویو(?:ی)?\s*باز\s*$",
        normalized_text,
    ):
        attributes.append(
            {
                "attribute_key": "open_view",
                "attribute_label": "ویو",
                "attribute_value": "باز",
                "display_order": 31,
            }
        )

    if re.search(
        r"(?mi)(?:^|\n)\s*"
        r"(?:✨️?|〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾|🌱)?\s*"
        r"(?:"
        r"عکس(?:[‌ \t]*ها|[‌ \t]*های)?\s*مربوط\s*به\s*خود\s*واحد(?:\s*(?:میباشد|می\s*باشد|است))?"
        r"|عکس(?:[‌ \t]*ها|[‌ \t]*های)?\s*واقعی\s*(?:خود\s*)?واحد"
        r"|تصاویر\s*مربوط\s*به\s*خود\s*واحد(?:\s*(?:میباشد|می\s*باشد|است))?"
        r")\s*$",
        normalized_text,
    ):
        attributes.append(
            {
                "attribute_key": "source_photo_authenticity",
                "attribute_label": "عکس‌ها",
                "attribute_value": "مربوط به خود واحد",
                "display_order": 32,
            }
        )

    units_match = re.search(
        r"(?mi)^\s*"
        r"(?:〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾)?\s*"
        r"تعداد\s*واحد\s*در\s*طبقه"
        r"\s*[:：\-]?\s*"
        r"([0-9۰-۹٠-٩]+)",
        normalized_text,
    )

    if not units_match:
        # «۵ واحد در طبقه» puts the number first.
        units_match = re.search(
            r"(?mi)^\s*"
            r"(?:〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾)?\s*"
            r"([0-9]+)\s*واحد\s*در\s*(?:هر\s*)?طبقه\s*$",
            normalized_text,
        )

    if units_match:
        value = units_match.group(1)

        value = value.translate(
            str.maketrans(
                "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                "01234567890123456789",
            )
        )

        attributes.append(
            {
                "attribute_key": "units_per_floor",
                "attribute_label": "تعداد واحد در طبقه",
                "attribute_value": value,
                "display_order": 25,
            }
        )

    freeform_features = [
        (
            "master_bedroom",
            "خواب مستر",
            r"(?mi)^.*خواب\s*مستر.*$",
            40,
        ),
        (
            "balcony",
            "بالکن",
            r"(?mi)^.*بالکن.*$",
            41,
        ),
        (
            "terrace_kitchen",
            "آشپزخانه",
            r"(?mi)^.*آشپزخانه.*رو\s*به\s*تراس.*$",
            42,
        ),
        (
            "closet_room",
            "کلوزت روم",
            r"(?mi)^.*کلوزت\s*روم.*$",
            43,
        ),
        (
            "lobby",
            "لابی",
            r"(?mi)^.*لابی.*$",
            44,
        ),
        (
            "cctv",
            "دوربین مداربسته",
            r"(?mi)^.*دوربین\s*مداربسته.*$",
            45,
        ),
        (
            "facade",
            "نما",
            r"(?mi)^\s*نما\s*[:：\-]?\s*(.+)$",
            46,
        ),
        (
            "no_patio",
            "پاسیو",
            r"(?mi)^.*بدون\s+پاسیو.*$",
            47,
        ),
        (
            "no_setback",
            "عقب‌روی",
            r"(?mi)^.*بدون\s+عقب\s*روی.*$",
            48,
        ),
        (
            "luxury_installations",
            "نصبیات",
            r"(?mi)^.*فول\s+نصبیات.*$",
            49,
        ),
        (
            "floor_covering",
            "کف",
            r"(?mi)^.*کف\s*(?:پارکت|سرامیک|سنگ).*?$",
            50,
        ),
        (
            "heating_system",
            "گرمایش",
            r"(?mi)^.*(?:گرمایش\s*)?(?:رادیات|رادیاتور).*?$",
            51,
        ),
        (
            "open_kitchen",
            "آشپزخانه",
            r"(?mi)^.*آشپزخانه\s*(?:اپن|اوپن).*?$",
            52,
        ),
        (
            "wall_boiler",
            "سیستم گرمایش",
            r"(?mi)^.*پکیج\s*دیواری.*?$",
            53,
        ),
        (
            "fireplace",
            "شومینه",
            r"(?mi)^.*شومینه.*?$",
            54,
        ),
        (
            "cabinet_wardrobe",
            "کمد و کابینت",
            r"(?mi)^.*کمد\s*کابینت.*?$",
            55,
        ),
        (
            "wallpaper",
            "کاغذ دیواری",
            r"(?mi)^.*کاغذ\s*دیواری.*?$",
            56,
        ),
        (
            "large_bathroom",
            "حمام",
            r"(?mi)^.*حمام\s*بزرگ.*?$",
            57,
        ),
        (
            "wall_bed",
            "تخت‌خواب دیواری",
            r"(?mi)^.*تخت\s*خواب\s*دیواری.*?$",
            58,
        ),
    ]

    existing_keys = {
        item["attribute_key"]
        for item in attributes
    }

    for (
        attribute_key,
        attribute_label,
        pattern,
        display_order,
    ) in freeform_features:

        if attribute_key in existing_keys:
            continue

        if (
            attribute_key == "luxury_installations"
            and "installations" in existing_keys
        ):
            continue

        match = re.search(
            pattern,
            normalized_text,
        )

        if not match:
            continue

        if (
            attribute_key == "facade"
            and match.lastindex
        ):
            value = match.group(1).strip()
        else:
            value = match.group(0).strip()

        # A description line can contain words such as
        # lobby, balcony, fireplace, etc.  Those words
        # must not cause the whole description line to be
        # saved again as an individual feature.
        matched_line = match.group(0).strip()

        description_line = re.sub(
            r"^\s*(?:✨|〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾)?\s*",
            "",
            matched_line,
        ).strip()

        if re.match(
            r"^توضیحات\s*[:：]",
            description_line,
        ):
            continue

        attributes.append(
            {
                "attribute_key": attribute_key,
                "attribute_label": attribute_label,
                "attribute_value": value[:150],
                "display_order": display_order,
            }
        )

    description_match = re.search(
        r"(?mi)^\s*"
        r"(?:〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾)?\s*"
        r"توضیحات\s*[:：]\s*"
        r"([^\n\r]+)$",
        normalized_text,
    )

    if description_match:
        value = description_match.group(1).strip()

        value = re.sub(
            r"\s*-\s*",
            " - ",
            value,
        )

        # These standard features are already displayed
        # separately by the publisher. Remove them from
        # the description to avoid duplicate output.
        standard_description_features = (
            "دارای پارکینگ",
            "دارای آسانسور",
            "دارای انباری",
        )

        parts = [
            part.strip()
            for part in value.split("-")
            if part.strip()
        ]

        parts = [
            part
            for part in parts
            if not any(
                feature in part
                for feature in standard_description_features
            )
        ]

        value = " - ".join(parts)

        value = re.sub(
            r"\s+",
            " ",
            value,
        ).strip()

        if value:
            attributes.append(
                {
                    "attribute_key": "property_description",
                    "attribute_label": "توضیحات",
                    "attribute_value": value[:300],
                    "display_order": 90,
                }
            )

    # -------------------------------------------------
    # Generic structured-attribute fallback
    #
    # Preserve useful source lines such as:
    #   طراحی: فوق‌لوکس
    #   کابینت: چوب راش
    #   سیستم گرمایشی: گرمایش از کف
    #   موقعیت: دوکله
    #
    # without requiring a new regex for every future feature.
    # -------------------------------------------------

    existing_labels = {
        str(item.get("attribute_label") or "").strip()
        for item in attributes
    }

    ignored_labels = {
        "کد واحد",
        "کد فایل",
        "کد ملک",
        "کد آگهی",
        "متراژ",
        "متراژ واحد",
        "تعداد خواب",
        "خواب",
        "طبقه",
        "سال ساخت",
        "ساخت",
        "پایان کار",
        "پایانکار",
        "منطقه",
        "منطقه واحد",
        "لوکیشن",
        "آدرس",
        "قیمت",
        "قیمت کل",
        "قیمت هر متر",
        "قیمت متری",
        "مبلغ",
        "مبلغ کل",
        "پارکینگ",
        "آسانسور",
        "انباری",
        "جهت",
        "جهت واحد",
        "کانال تلگرام",
        "آیدی",
        "تلفن",
        "شماره تماس",
        "تماس",
        "آدرس دفتر",
        "http",
        "https",
        "نام مشاور",
        "مشاور",
        "مشاور فروش",
        "کارشناس فروش",
        "مسئول فروش",
        "مدیر فروش",
        "سند",
        "وضعیت",
        "نصبیات",
        "توضیحات",
        "نما",
    }

    generic_order = 60

    for raw_line in normalized_text.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        # Never interpret URLs as structured attributes.
        if re.match(
            r"^https?://",
            line,
            flags=re.IGNORECASE,
        ):
            continue

        line = re.sub(
            r"^\s*(?:✨|〽️|🔹|🔸|♦️|♦|▪️|▪|•|◾|📍|📋|📞|📱|📢|👨‍💼|🏠)\s*",
            "",
            line,
        ).strip()

        match = re.match(
            r"^([^:：]{2,40})\s*[:：]\s*(.+?)\s*$",
            line,
        )

        if not match:
            continue

        label = re.sub(
            r"\s+",
            " ",
            match.group(1),
        ).strip()

        # Remove decorative emojis/symbols from the
        # beginning of labels before blacklist matching.
        #
        # Examples:
        #   🏡کد آگهی   -> کد آگهی
        #   ⚜️ قیمت     -> قیمت
        label = re.sub(
            r"^[^0-9A-Za-zآ-ی]+",
            "",
            label,
        ).strip()

        value = re.sub(
            r"\s+",
            " ",
            match.group(2),
        ).strip()

        if not label or not value:
            continue

        if label in ignored_labels:
            continue

        if label in existing_labels:
            continue

        # Prevent contact / marketing / metadata lines
        combined = f"{label} {value}"

        blocked_markers = (
            "@",
            "091",
            "092",
            "093",
            "090",
            "جهت کسب اطلاعات",
            "هماهنگی بازدید",
            "سرمایه‌گذاری",
            "کانال",
            "تلگرام",
        )

        if any(
            marker in combined
            for marker in blocked_markers
        ):
            continue

        key_hash = hashlib.sha1(
            label.encode("utf-8")
        ).hexdigest()[:12]

        attributes.append(
            {
                "attribute_key": (
                    f"generic_{key_hash}"
                ),
                "attribute_label": label[:80],
                "attribute_value": value[:200],
                "display_order": generic_order,
            }
        )

        existing_labels.add(label)
        generic_order += 1

    # === PRE-SALE / DELIVERY FIDELITY V2 ===
    if re.search(
        r"(?i)(?:پیش[ _‌\-]*فروش|#پیش[_‌\-]*فروش)",
        normalized_text,
    ):
        attributes.append(
            {
                "attribute_key": "sale_mode",
                "attribute_label": "نوع عرضه",
                "attribute_value": "پیش‌فروش",
                "display_order": 33,
            }
        )

    # === SOURCE UNLABELLED PROPERTY DETAILS V2 ===
    #
    # Preserve useful unlabelled source lines such as:
    #   ✨ ابعاد 18*19
    #   ✨ دارای رضایت 70 درصد از طرفین
    #   ✨ دو طبقه ساخت قابل نشیمن
    #
    # Contact, marketing, price and metadata lines are excluded.

    existing_detail_values = {
        re.sub(
            r"\s+",
            " ",
            str(
                item.get("attribute_value")
                or ""
            ),
        ).strip()
        for item in attributes
        if item.get("attribute_value")
    }

    detail_signals = (
        "ابعاد",
        "رضایت",
        "قابل نشیمن",
        "قابل سکونت",
        "قابل ساخت",
        "طبقه ساخت",
        "طبقات ساخت",
        "مجوز ساخت",
        "پروانه ساخت",
        "تراکم",
        "کاربری",
        "قدرالسهم",
        "عرصه",
        "اعیان",
        "اصلاحی",
        "عقب روی",
        "عقب‌روی",
        "گذر",
        "بر ملک",
        "عرض",
        "طول",
        "نبش",
        "دوکله",
        "دو کله",
        "سه بر",
        "تفکیک",
        "تفکیکی",
        "موقعیت",
        "نورگیر",
        "ویو",
        "پاسیو",
        "پنجره",
        "سازنده",
        "بالکن",
        "تراس",
        "مستر",
        "کلوزت",
        "لابی",
        "شومینه",
        "کابینت",
        "گرمایش",
        "سرمایش",
        "پکیج",
        "پارکت",
        "سرامیک",
        "سنگ",
        "بازسازی",
        "نقشه",
        "تحویل",
        "شرایط پرداخت",
        "پیش پرداخت",
        "پیش‌پرداخت",
        "پرداخت",
        "اقساط",
        "قسط",
        "سند و تحویل",
        "درصد",
        "تک واحدی",
        "تک‌واحدی",
        "دو واحدی",
        "دو‌واحدی",
        "واحدی",
        "پیش فروش",
        "پیش‌فروش",
    )

    blocked_detail_markers = (
        "قیمت",
        "شماره تماس",
        "تماس",
        "مشاور",
        "کانال",
        "تلگرام",
        "اینستاگرام",
        "جهت کسب اطلاعات",
        "هماهنگی بازدید",
        "سرمایه گذاری",
        "سرمایه‌گذاری",
        "فروشنده",
        "مالک",
        "ادمین",
        "http://",
        "https://",
        "t.me/",
        "@",
    )

    source_detail_order = 80

    for raw_line in normalized_text.splitlines():
        original_line = raw_line.strip()

        if not original_line:
            continue

        had_property_bullet = bool(
            re.match(
                r"^\s*(?:"
                r"✨️?|✨|〽️|🔹|🔸|"
                r"♦️|♦|▪️|▪|•|◾|🌱"
                r")\s*",
                original_line,
            )
        )

        line = re.sub(
            r"^\s*(?:"
            r"✨️?|✨|〽️|🔹|🔸|"
            r"♦️|♦|▪️|▪|•|◾|🌱"
            r")\s*",
            "",
            original_line,
        ).strip()

        line = re.sub(
            r"\s+",
            " ",
            line,
        ).strip()

        if not line:
            continue

        # Hashtags / Telegram metadata.
        if line.startswith("#"):
            continue

        # Structured "label: value" lines are already handled
        # by the existing structured fallback.
        if ":" in line or "：" in line:
            continue

        if any(
            marker in line
            for marker in blocked_detail_markers
        ):
            continue

        # Phone numbers.
        if re.search(
            r"(?:^|\D)"
            r"0?9\d{9}"
            r"(?:\D|$)",
            line,
        ):
            continue

        # Skip plain source-code / file-code lines.
        if re.search(
            r"(?i)\b"
            r"(?:کد\s*(?:واحد|فایل|ملک|آگهی))"
            r"\b",
            line,
        ):
            continue

        # Skip a core line such as:
        #   قطعه 345 متری شمالی
        # because area/direction are already rendered separately.
        if re.fullmatch(
            r"(?:قطعه|زمین|آپارتمان|واحد)?"
            r"\s*[0-9۰-۹٠-٩]+"
            r"(?:[./,][0-9۰-۹٠-٩]+)?"
            r"\s*(?:متر|متری)"
            r"(?:\s*(?:شمالی|جنوبی|شرقی|غربی))?"
            r"\s*",
            line,
        ):
            continue

        has_dimension = bool(
            re.search(
                r"[0-9۰-۹٠-٩]+\s*"
                r"(?:×|x|X|\*)\s*"
                r"[0-9۰-۹٠-٩]+",
                line,
            )
        )

        has_percentage = bool(
            re.search(
                r"[0-9۰-۹٠-٩]+\s*"
                r"(?:درصد|٪)",
                line,
            )
        )

        has_signal = any(
            signal in line
            for signal in detail_signals
        )

        # To avoid importing ordinary prose/marketing:
        # keep only a meaningful property-detail line.
        if not (
            has_signal
            or has_dimension
            or (
                had_property_bullet
                and has_percentage
            )
        ):
            continue

        # Normalize dimensions for cleaner output.
        clean_line = re.sub(
            r"(?<=[0-9۰-۹٠-٩])"
            r"\s*(?:x|X|\*)\s*"
            r"(?=[0-9۰-۹٠-٩])",
            "×",
            line,
        )

        clean_line = re.sub(
            r"\s+",
            " ",
            clean_line,
        ).strip()

        # Do not duplicate an attribute already extracted
        # by one of the dedicated parsers above.
        duplicate = False

        for existing_value in existing_detail_values:
            if not existing_value:
                continue

            if (
                clean_line == existing_value
                or clean_line in existing_value
                or existing_value in clean_line
            ):
                duplicate = True
                break

        if duplicate:
            continue

        key_hash = hashlib.sha1(
            clean_line.encode("utf-8")
        ).hexdigest()[:12]

        attributes.append(
            {
                "attribute_key": (
                    f"source_detail_{key_hash}"
                ),
                "attribute_label": "جزئیات",
                "attribute_value": clean_line[:220],
                "display_order": source_detail_order,
            }
        )

        existing_detail_values.add(
            clean_line
        )

        source_detail_order += 1


    # Never republish a source's own channels, links or contact handles.
    return [
        item
        for item in attributes
        if not is_promotional_attribute(
            item.get("attribute_label"),
            item.get("attribute_value"),
        )
    ]


def parse_rule_attributes(text: str, rules):
    """
    Parse user-defined dynamic property attributes.

    Supported match types:
    - PRESENCE
    - VALUE_AFTER
    - NUMBER_BEFORE

    VALUE_AFTER is intentionally strict:
    the trigger must appear at the beginning of a specification line
    (optionally after a bullet/emoji) and must be followed by a separator.

    Examples accepted:
        ویو: کوه
        🔹 ویو: ائل‌گلی
        ♦️ ویو - باز

    Example rejected:
        ویو ابدی همیشه جزو گزینه‌های قابل توجه بازار هستن.
    """
    text = primary_listing_text(text or "")

    normalized_text = normalize_text(text or "")
    attributes = []

    bullet_pattern = r"(?:🔹|🔸|♦️|♦|▪️|▪|•|◾)?"

    for rule in rules or []:
        attribute_key = rule.get("attribute_key")
        attribute_label = rule.get("attribute_label")
        match_type = rule.get("match_type")
        trigger_text = rule.get("trigger_text")
        default_value = rule.get("default_value")
        display_order = rule.get("display_order", 100)

        if not attribute_key or not attribute_label:
            continue

        if not trigger_text:
            continue

        trigger = normalize_text(
            str(trigger_text)
        ).strip()

        if not trigger:
            continue

        value = None

        # -------------------------------------------------
        # PRESENCE
        # -------------------------------------------------
        if match_type == "PRESENCE":
            if trigger in normalized_text:
                value = default_value or "دارد"

        # -------------------------------------------------
        # VALUE AFTER
        # -------------------------------------------------
        elif match_type == "VALUE_AFTER":
            #
            # Trigger must be at the START of a line
            # and followed by :, ：, -, | or =
            #
            pattern = (
                r"(?m)^"
                r"\s*"
                + bullet_pattern
                + r"\s*"
                + re.escape(trigger)
                + r"\s*"
                + r"[:：=\-|]"
                + r"\s*"
                + r"([^\n\r]+)"
                + r"$"
            )

            match = re.search(
                pattern,
                normalized_text,
            )

            if match:
                value = match.group(1).strip()

                # Remove trailing specification bullets
                value = re.split(
                    r"\s*(?:🔹|🔸|♦️|♦|▪️|▪|•|◾)\s*",
                    value,
                    maxsplit=1,
                )[0].strip()

        # -------------------------------------------------
        # NUMBER BEFORE
        # -------------------------------------------------
        elif match_type == "NUMBER_BEFORE":
            pattern = (
                r"(?m)^"
                r"\s*"
                + bullet_pattern
                + r"\s*"
                + r"(\d+(?:\.\d+)?)"
                + r"\s*"
                + re.escape(trigger)
                + r"(?:\s|$|[:：\-])"
            )

            match = re.search(
                pattern,
                normalized_text,
            )

            if match:
                value = match.group(1).strip()

        if value is None:
            continue

        value = str(value).strip()

        if not value:
            continue

        attributes.append(
            {
                "attribute_key": attribute_key,
                "attribute_label": attribute_label,
                "attribute_value": value,
                "display_order": display_order,
            }
        )

    return attributes


def parse_source_unit_code(text: str):
    text = normalize_text(text)

    patterns = [
        r"کد\s*فایل\s*[:：\-]?\s*(\d+)",
        r"کد\s*واحد\s*[:：\-]?\s*(\d+)",
        r"کد\s*ملک\s*[:：\-]?\s*(\d+)",
        r"کد\s*آگهی\s*[:：\-]?\s*(\d+)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        if match:
            return match.group(1)

    return None


# === SOURCE FIDELITY ROOT FIX V2 ===
