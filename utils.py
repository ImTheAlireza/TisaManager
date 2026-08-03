"""Small helpers shared by handlers."""

from html import escape
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import time

try:  # config pulls in python-dotenv, which the unit tests do not install.
    from config import DISPLAY_TIMEZONE, WORKFLOW_TTL_SECONDS
except Exception:  # pragma: no cover - fallback for bare test environments
    DISPLAY_TIMEZONE = "Asia/Tehran"
    WORKFLOW_TTL_SECONDS = 30 * 60

STATE_TTL_SECONDS = WORKFLOW_TTL_SECONDS

# Single source of truth for the user-facing timezone. Storage is always UTC.
LOCAL_TZ = ZoneInfo(DISPLAY_TIMEZONE)
UTC = timezone.utc


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
    """Render a stored naive-UTC datetime in the display timezone."""
    local = utc_naive_to_local(utc_dt)
    return local.strftime(fmt) if local else "—"

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


GROUP_NOTICE = "⛔️ این ربات فقط در چت خصوصی کار می‌کند."


def state_is_expired(state: dict) -> bool:
    return time.monotonic() - state.get("created_at", 0) > STATE_TTL_SECONDS
