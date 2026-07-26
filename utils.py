"""Small helpers shared by handlers."""

from html import escape


import time

STATE_TTL_SECONDS = 30 * 60


def html_text(value: object) -> str:
    """Escape user-controlled text before putting it in an HTML Telegram message."""
    return escape("" if value is None else str(value), quote=False)


def state_is_expired(state: dict) -> bool:
    return time.monotonic() - state.get("created_at", 0) > STATE_TTL_SECONDS
