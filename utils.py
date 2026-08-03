"""Small helpers shared by handlers."""

from html import escape
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import time

import jalali

try:  # config pulls in python-dotenv, which the unit tests do not install.
    from config import DISPLAY_TIMEZONE, WORKFLOW_TTL_SECONDS, USE_JALALI
except Exception:  # pragma: no cover - fallback for bare test environments
    DISPLAY_TIMEZONE = "Asia/Tehran"
    WORKFLOW_TTL_SECONDS = 30 * 60
    USE_JALALI = True

STATE_TTL_SECONDS = WORKFLOW_TTL_SECONDS

# Single source of truth for the user-facing timezone. Storage is always UTC.
LOCAL_TZ = ZoneInfo(DISPLAY_TIMEZONE)
UTC = timezone.utc


def set_calendar(use_jalali: bool):
    """Switch the display calendar at runtime.

    ``USE_JALALI`` is read as a module global by the formatters below, so the
    owner-facing settings toggle can change it without a restart. Only display
    is affected; stored values remain Gregorian UTC.
    """
    global USE_JALALI
    USE_JALALI = bool(use_jalali)


def now_local() -> datetime:
    """Timezone-aware 'now' in the display timezone."""
    return datetime.now(LOCAL_TZ)


def local_to_utc_naive(local_dt: datetime) -> datetime:
    """Convert a display-timezone datetime to the naive UTC we store.

    Accepts naive input (interpreted as local) or aware input. ``fold=0`` keeps
    the first occurrence of an ambiguous local time during a DST rollback,
    matching what the user saw on the keyboard.
    """
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=LOCAL_TZ)
    return local_dt.astimezone(UTC).replace(tzinfo=None)


def utc_naive_to_local(utc_dt: datetime) -> datetime:
    """Convert a stored naive-UTC datetime back to display-timezone."""
    if utc_dt is None:
        return None
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=UTC)
    return utc_dt.astimezone(LOCAL_TZ)


def format_local(utc_dt: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render a stored naive-UTC datetime in the display timezone.

    Honours ``USE_JALALI``: the UI is entirely Persian, so dates are shown on
    the Persian calendar by default. Storage is unaffected — always Gregorian
    UTC.
    """
    local = utc_naive_to_local(utc_dt)
    if not local:
        return "—"
    if USE_JALALI:
        return jalali.format_jalali_datetime(local)
    return local.strftime(fmt)


def format_local_short(utc_dt: datetime) -> str:
    """Compact 'MM/DD HH:MM' for list rows. Input is stored naive UTC."""
    local = utc_naive_to_local(utc_dt)
    if not local:
        return ""
    if USE_JALALI:
        _, jm, jd = jalali.to_jalali(local)
        return jalali.to_persian_digits(f"{jm:02d}/{jd:02d} {local:%H:%M}")
    return local.strftime("%m/%d %H:%M")


def format_local_date(value, long_form: bool = False) -> str:
    """Render a date (already local) for display."""
    if value is None:
        return "—"
    if USE_JALALI:
        return (jalali.format_jalali_long(value) if long_form
                else jalali.format_jalali_date(value))
    return value.strftime("%Y-%m-%d")


def format_clock(hour: int, minute: int) -> str:
    """Render an HH:MM clock time in the display digit set."""
    text = f"{hour:02d}:{minute:02d}"
    return jalali.to_persian_digits(text) if USE_JALALI else text


def date_example() -> str:
    """A copy-pasteable example of the accepted manual-entry format."""
    today = now_local().date()
    if USE_JALALI:
        return f"{jalali.format_jalali_date(today, persian_digits=False)} 14:30"
    return f"{today:%Y-%m-%d} 14:30"


def parse_user_datetime(text: str) -> datetime:
    """Parse a user-typed 'date time' string into a naive local datetime.

    Accepts the Jalali calendar (the default for this UI) and, as a
    convenience, unambiguous Gregorian input: a year >= 1700 can only be
    Gregorian, so both are supported without guessing.
    Persian and Arabic-Indic digits are normalised first.
    """
    raw = jalali.to_latin_digits(text.strip())
    parts = raw.split()
    if len(parts) < 2:
        raise ValueError("زمان را هم بنویسید")
    date_part, time_part = parts[0], parts[1]

    time_bits = time_part.split(":")
    if len(time_bits) < 2:
        raise ValueError("ساعت نامعتبر است")
    try:
        hour, minute = int(time_bits[0]), int(time_bits[1])
    except ValueError:
        raise ValueError("ساعت باید عددی باشد") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("ساعت باید بین ۰۰:۰۰ تا ۲۳:۵۹ باشد")

    normalised = date_part.replace("/", "-").replace(".", "-")
    chunks = [c for c in normalised.split("-") if c]
    if len(chunks) != 3:
        raise ValueError("قالب تاریخ نامعتبر است")
    try:
        year = int(chunks[0])
    except ValueError:
        raise ValueError("تاریخ باید عددی باشد") from None

    if year >= 1700:
        # Unambiguously Gregorian.
        day = datetime.strptime(normalised, "%Y-%m-%d").date()
    else:
        day = jalali.parse_jalali_date(normalised)
    return datetime(day.year, day.month, day.day, hour, minute)

# Telegram chat type for one-to-one conversations. Kept as a plain string so
# this module stays importable without the telegram package (see tests).
PRIVATE_CHAT_TYPE = "private"


def html_text(value: object) -> str:
    """Escape user-controlled text before putting it in an HTML Telegram message."""
    return escape("" if value is None else str(value), quote=False)


def is_private_chat(update) -> bool:
    """Return True only for one-to-one private chats.

    Everything else — ``group``, ``supergroup`` and ``channel`` — is rejected so
    interactive workflows can never be driven from a shared chat. Uses ``getattr``
    because channel posts and some service updates carry no ``effective_chat``.
    """
    return getattr(getattr(update, "effective_chat", None), "type", None) == PRIVATE_CHAT_TYPE


def private_actor(update):
    """Return the acting user for a private-chat update, or ``None`` to ignore it.

    Guards the three things that make group/channel updates crash handlers:
    a non-private chat, a missing ``effective_user`` (anonymous channel posts) and
    a missing ``message`` (edited messages and other non-message updates).
    """
    if not is_private_chat(update):
        return None
    if getattr(update, "message", None) is None:
        return None
    return getattr(update, "effective_user", None)


def display_name(row) -> str:
    """Best human label for a user row: name, then @username, then id.

    Accepts a dict (DB row) or an object with the same attributes. Users are
    added by numeric id, so ``name`` may be NULL until the bot has seen that
    person act; falling back through username to id keeps every screen
    readable instead of showing a bare number.
    """
    if row is None:
        return "—"
    get = row.get if isinstance(row, dict) else lambda k, d=None: getattr(row, k, d)
    name = (get("name") or "").strip()
    username = (get("username") or "").strip().lstrip("@")
    user_id = get("user_id") or get("id")
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return f"#{user_id}" if user_id is not None else "—"


def telegram_display_name(user) -> str:
    """Full name from a Telegram User, falling back to @username."""
    if user is None:
        return ""
    full = getattr(user, "full_name", None)
    if not full:
        parts = [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or ""]
        full = " ".join(p for p in parts if p).strip()
    return full or (getattr(user, "username", "") or "")


GROUP_NOTICE = "⛔️ این ربات فقط در چت خصوصی کار می‌کند."


def state_is_expired(state: dict) -> bool:
    return time.monotonic() - state.get("created_at", 0) > STATE_TTL_SECONDS
