"""Small helpers shared by handlers."""

from html import escape


import time

STATE_TTL_SECONDS = 30 * 60

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


def state_is_expired(state: dict) -> bool:
    return time.monotonic() - state.get("created_at", 0) > STATE_TTL_SECONDS
