"""Jalali (Solar Hijri) calendar conversion.

Implemented in-house on purpose: the rest of this project avoids third-party
dependencies (see ``bale_client``, which speaks HTTP over ``urllib``), and the
conversion is a closed-form integer algorithm with no external data.

The algorithm is the standard Pournader/Toossi one, exact for Jalali years
1178-1633 (Gregorian ~1799-2255). ``tests/test_jalali.py`` checks every single
day from 1990 to 2100 against the ``jdatetime`` reference implementation.

Storage stays Gregorian/UTC everywhere; these helpers are for display and for
parsing what the user types.
"""

from datetime import date

# Persian month names, index 1..12.
MONTH_NAMES = (
    "", "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)

# Persian weekday names. Index matches ``date.weekday()`` (Monday == 0).
WEEKDAY_NAMES = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")

# Persian-Indic digits, for rendering numbers the way a Persian reader expects.
_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_LATIN_DIGITS = "0123456789"
_TO_PERSIAN = str.maketrans(_LATIN_DIGITS, _PERSIAN_DIGITS)
# Accept Persian *and* Arabic-Indic digits on input: phone keyboards emit both.
_TO_LATIN = str.maketrans(
    _PERSIAN_DIGITS + "٠١٢٣٤٥٦٧٨٩",
    _LATIN_DIGITS + _LATIN_DIGITS,
)

_G_DAYS_IN_MONTH = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


def to_persian_digits(value) -> str:
    """Render a value with Persian-Indic digits."""
    return str(value).translate(_TO_PERSIAN)


def to_latin_digits(value: str) -> str:
    """Normalise Persian/Arabic digits to ASCII so int() can parse them."""
    return str(value).translate(_TO_LATIN)


def is_leap_jalali(jy: int) -> bool:
    """True when the Jalali year has 366 days (Esfand has 30).

    Uses the 33-year cycle: years whose remainder mod 33 is one of
    {1, 5, 9, 13, 17, 22, 26, 30} are leap.
    """
    return (jy % 33) in (1, 5, 9, 13, 17, 22, 26, 30)


def days_in_jalali_month(jy: int, jm: int) -> int:
    """Length of a Jalali month: 31, 30, or 29/30 for Esfand."""
    if not 1 <= jm <= 12:
        raise ValueError(f"month out of range: {jm}")
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    return 30 if is_leap_jalali(jy) else 29


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert a Gregorian (y, m, d) to Jalali (y, m, d)."""
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        - 80
        + gd
        + _G_DAYS_IN_MONTH[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    """Convert a Jalali (y, m, d) to Gregorian (y, m, d)."""
    if jy > 979:
        gy = 1600
        jy -= 979
    else:
        gy = 621
    days = (
        365 * jy
        + (jy // 33) * 8
        + (jy % 33 + 3) // 4
        + 78
        + jd
        + (186 if jm > 6 else (jm - 1) * 31)
    )
    if jm > 6:
        days += (jm - 7) * 30
    gy += 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)
    sal_a = [
        0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31,
    ]
    gm = 0
    while gm < 13 and gd > sal_a[gm]:
        gd -= sal_a[gm]
        gm += 1
    return gy, gm, gd


def to_jalali(value: date) -> tuple[int, int, int]:
    """Jalali (y, m, d) for a ``date``/``datetime``."""
    return gregorian_to_jalali(value.year, value.month, value.day)


def from_jalali(jy: int, jm: int, jd: int) -> date:
    """``date`` for a Jalali (y, m, d). Raises ValueError when out of range."""
    if not 1 <= jm <= 12:
        raise ValueError(f"ماه نامعتبر است: {to_persian_digits(jm)}")
    limit = days_in_jalali_month(jy, jm)
    if not 1 <= jd <= limit:
        raise ValueError(
            f"روز نامعتبر است: {to_persian_digits(jd)} "
            f"(«{MONTH_NAMES[jm]}» {to_persian_digits(limit)} روز دارد)"
        )
    gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
    return date(gy, gm, gd)


def format_jalali_date(value: date, persian_digits: bool = True) -> str:
    """'۱۴۰۵-۰۵-۱۲' style date."""
    jy, jm, jd = to_jalali(value)
    out = f"{jy:04d}-{jm:02d}-{jd:02d}"
    return to_persian_digits(out) if persian_digits else out


def format_jalali_long(value: date, persian_digits: bool = True) -> str:
    """'۱۲ مرداد ۱۴۰۵' style date."""
    jy, jm, jd = to_jalali(value)
    day = to_persian_digits(jd) if persian_digits else str(jd)
    year = to_persian_digits(jy) if persian_digits else str(jy)
    return f"{day} {MONTH_NAMES[jm]} {year}"


def format_jalali_datetime(value, persian_digits: bool = True) -> str:
    """'۱۴۰۵-۰۵-۱۲ ۱۴:۳۰' style timestamp."""
    stamp = f"{format_jalali_date(value, persian_digits=False)} {value.hour:02d}:{value.minute:02d}"
    return to_persian_digits(stamp) if persian_digits else stamp


def weekday_name(value: date) -> str:
    return WEEKDAY_NAMES[value.weekday()]


def parse_jalali_date(text: str) -> date:
    """Parse '1405-05-12' or '1405/5/12' (Persian digits welcome).

    Two-digit years are rejected rather than guessed: silently turning '05'
    into 1405 would be a nasty way to schedule a post to the wrong day.
    """
    raw = to_latin_digits(text.strip()).replace("/", "-").replace(".", "-")
    parts = [p for p in raw.split("-") if p]
    if len(parts) != 3:
        raise ValueError("قالب تاریخ نامعتبر است")
    try:
        jy, jm, jd = (int(p) for p in parts)
    except ValueError:
        raise ValueError("تاریخ باید عددی باشد") from None
    if jy < 1000:
        raise ValueError("سال را کامل بنویسید، مثلاً ۱۴۰۵")
    return from_jalali(jy, jm, jd)
