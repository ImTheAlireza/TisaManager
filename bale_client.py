"""Bale API client — Telegram-compatible API at https://tapi.bale.ai

A second Bale bot can be configured (BALE_TOKEN_2) as a backup sender.
Delivery attempts alternate between the bots — attempt 1 goes out through
bot 1, attempt 2 through bot 2, attempt 3 through bot 1, and so on — so a
rate-limited or blocked bot is swapped for a fresh one on the next try.
Without BALE_TOKEN_2 every attempt uses the primary bot.
"""

import json
import logging
import asyncio

from config import BALE_TOKEN, BALE_TOKEN_2

logger = logging.getLogger(__name__)


def _build_multipart(data=None, files=None) -> tuple:
    """Build a multipart/form-data body. Returns (content_type, body bytes).

    Kept separate from the HTTP layer so the encoding can be unit-tested —
    a missed .encode() here once crashed every Bale file upload client-side
    with 'can't concat str to bytes' before the request ever left the bot.
    """
    boundary = "----FormBoundary7MA4YWxkTrZu0gW"
    body = b""
    if data:
        for key, val in data.items():
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
            body += str(val).encode()
            body += b"\r\n"
    for key, (filename, filedata, content_type) in (files or {}).items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'.encode()
        body += f"Content-Type: {content_type}\r\n\r\n".encode()
        body += filedata
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


def _post(url, data=None, files=None):
    """Synchronous HTTP POST using urllib (no external deps)."""
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode

    if files:
        content_type, body = _build_multipart(data=data, files=files)
        req = Request(url, data=body, method="POST")
        req.add_header("Content-Type", content_type)
    elif data:
        encoded = urlencode(data).encode()
        req = Request(url, data=encoded, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    else:
        req = Request(url, method="POST")

    resp = urlopen(req, timeout=30)
    return json.loads(resp.read().decode())


class BaleClient:
    """One Bale bot and its API surface."""

    def __init__(self, token: str, name: str):
        self.token = token
        self.name = name
        self.base_url = f"https://tapi.bale.ai/bot{token}"

    async def _request_async(self, method, data=None, files=None):
        """Run the legacy urllib client without blocking the bot event loop."""
        return await asyncio.to_thread(self._request, method, data, files)

    def _request(self, method, data=None, files=None):
        url = f"{self.base_url}/{method}"
        try:
            result = _post(url, data=data, files=files)
            if not result.get("ok"):
                logger.error("Bale API error on %s (%s): %s", method, self.name, result)
            else:
                logger.info("Bale API success on %s (%s)", method, self.name)
            return result
        except Exception as e:
            logger.error("Bale API request failed on %s (%s): %s", method, self.name, e)
            return {"ok": False, "description": str(e)}

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        data = {"chat_id": chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        return await self._request_async("sendMessage", data)

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None):
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if parse_mode:
            data["parse_mode"] = parse_mode
        if isinstance(photo, bytes):
            files = {"photo": ("photo.jpg", photo, "image/jpeg")}
            return await self._request_async("sendPhoto", data, files=files)
        else:
            data["photo"] = photo
            return await self._request_async("sendPhoto", data)

    async def send_video(self, chat_id, video, caption=None, parse_mode=None):
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if parse_mode:
            data["parse_mode"] = parse_mode
        if isinstance(video, bytes):
            files = {"video": ("video.mp4", video, "video/mp4")}
            return await self._request_async("sendVideo", data, files=files)
        else:
            data["video"] = video
            return await self._request_async("sendVideo", data)

    async def send_document(self, chat_id, document, caption=None, parse_mode=None):
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if parse_mode:
            data["parse_mode"] = parse_mode
        if isinstance(document, bytes):
            files = {"document": ("document.bin", document, "application/octet-stream")}
            return await self._request_async("sendDocument", data, files=files)
        else:
            data["document"] = document
            return await self._request_async("sendDocument", data)

    async def send_media_group(self, chat_id, media_files, caption=None):
        """Send media group with file uploads via attach:// syntax.

        media_files: list of (type, file_bytes) tuples
        """
        media_items = []
        files = {}
        for i, (media_type, file_bytes) in enumerate(media_files):
            field_name = f"file_{i}"
            ext = "jpg" if media_type == "photo" else "mp4"
            content_type = "image/jpeg" if media_type == "photo" else "video/mp4"
            files[field_name] = (f"{field_name}.{ext}", file_bytes, content_type)
            item = {"type": media_type, "media": f"attach://{field_name}"}
            if i == 0 and caption:
                item["caption"] = caption
            media_items.append(item)

        data = {"chat_id": chat_id, "media": json.dumps(media_items)}
        return await self._request_async("sendMediaGroup", data, files=files)

    async def get_chat(self, chat_id):
        return await self._request_async("getChat", {"chat_id": chat_id})

    async def get_me(self):
        return await self._request_async("getMe")

    async def edit_message_text(self, chat_id, message_id, text):
        return await self._request_async("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})

    async def edit_message_caption(self, chat_id, message_id, caption):
        return await self._request_async("editMessageCaption", {"chat_id": chat_id, "message_id": message_id, "caption": caption})

    async def delete_message(self, chat_id, message_id):
        return await self._request_async("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


# Bot 1 is the primary sender; bot 2 (if configured) is the backup that
# takes over every other attempt.
DEFAULT_CLIENT = BaleClient(BALE_TOKEN, "bale-1") if BALE_TOKEN else None
BACKUP_CLIENT = BaleClient(BALE_TOKEN_2, "bale-2") if BALE_TOKEN_2 else None


def client_for_attempt(attempt_no: int):
    """The Bale bot in charge of a (1-based) delivery attempt.

    Attempts alternate between the bots: 1 -> bot 1, 2 -> bot 2, 3 -> bot 1,
    ... Without a backup token every attempt uses the primary bot.
    """
    if BACKUP_CLIENT is not None and attempt_no % 2 == 0:
        return BACKUP_CLIENT
    return DEFAULT_CLIENT


def all_clients() -> list:
    """Every configured Bale bot, primary first."""
    return [c for c in (DEFAULT_CLIENT, BACKUP_CLIENT) if c is not None]


# --- Backwards-compatible module-level API (primary bot) --------------------

def _require_default():
    if DEFAULT_CLIENT is None:
        raise RuntimeError("BALE_TOKEN not configured")
    return DEFAULT_CLIENT


async def send_message(chat_id, text, parse_mode=None, reply_markup=None):
    return await _require_default().send_message(chat_id, text, parse_mode, reply_markup)


async def send_photo(chat_id, photo, caption=None, parse_mode=None):
    return await _require_default().send_photo(chat_id, photo, caption, parse_mode)


async def send_video(chat_id, video, caption=None, parse_mode=None):
    return await _require_default().send_video(chat_id, video, caption, parse_mode)


async def send_document(chat_id, document, caption=None, parse_mode=None):
    return await _require_default().send_document(chat_id, document, caption, parse_mode)


async def send_media_group(chat_id, media_files, caption=None):
    return await _require_default().send_media_group(chat_id, media_files, caption)


async def get_chat(chat_id):
    return await _require_default().get_chat(chat_id)


async def get_me():
    return await _require_default().get_me()


async def edit_message_text(chat_id, message_id, text):
    return await _require_default().edit_message_text(chat_id, message_id, text)


async def edit_message_caption(chat_id, message_id, caption):
    return await _require_default().edit_message_caption(chat_id, message_id, caption)


async def delete_message(chat_id, message_id):
    return await _require_default().delete_message(chat_id, message_id)
