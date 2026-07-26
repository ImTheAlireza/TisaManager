"""Bale API client — Telegram-compatible API at https://tapi.bale.ai"""

import json
import logging
import tempfile
import os
import asyncio

from config import BALE_TOKEN

logger = logging.getLogger(__name__)

BASE_URL = f"https://tapi.bale.ai/bot{BALE_TOKEN}"


def _post(url, data=None, files=None):
    """Synchronous HTTP POST using urllib (no external deps)."""
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode

    if files:
        # Multipart form-data: data fields first, then files
        boundary = "----FormBoundary7MA4YWxkTrZu0gW"
        body = b""
        if data:
            for key, val in data.items():
                body += f"--{boundary}\r\n".encode()
                body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
                body += str(val).encode()
                body += b"\r\n"
        for key, (filename, filedata, content_type) in files.items():
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'.encode()
            body += f"Content-Type: {content_type}\r\n\r\n".encode()
            body += filedata
            body += b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = Request(url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    elif data:
        encoded = urlencode(data).encode()
        req = Request(url, data=encoded, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    else:
        req = Request(url, method="POST")

    resp = urlopen(req, timeout=30)
    return json.loads(resp.read().decode())


async def _request_async(method, data=None, files=None):
    """Run the legacy urllib client without blocking the bot event loop."""
    return await asyncio.to_thread(_request, method, data, files)


def _request(method, data=None, files=None):
    url = f"{BASE_URL}/{method}"
    try:
        result = _post(url, data=data, files=files)
        if not result.get("ok"):
            logger.error("Bale API error on %s: %s", method, result)
        else:
            logger.info("Bale API success on %s", method)
        return result
    except Exception as e:
        logger.error("Bale API request failed on %s: %s", method, e)
        return {"ok": False, "description": str(e)}


async def send_message(chat_id, text, parse_mode=None, reply_markup=None):
    data = {"chat_id": chat_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return await _request_async("sendMessage", data)


async def send_photo(chat_id, photo, caption=None, parse_mode=None):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if parse_mode:
        data["parse_mode"] = parse_mode
    if isinstance(photo, bytes):
        files = {"photo": ("photo.jpg", photo, "image/jpeg")}
        return await _request_async("sendPhoto", data, files=files)
    else:
        data["photo"] = photo
        return await _request_async("sendPhoto", data)


async def send_video(chat_id, video, caption=None, parse_mode=None):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if parse_mode:
        data["parse_mode"] = parse_mode
    if isinstance(video, bytes):
        files = {"video": ("video.mp4", video, "video/mp4")}
        return await _request_async("sendVideo", data, files=files)
    else:
        data["video"] = video
        return await _request_async("sendVideo", data)


async def send_document(chat_id, document, caption=None, parse_mode=None):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if parse_mode:
        data["parse_mode"] = parse_mode
    if isinstance(document, bytes):
        files = {"document": ("document.bin", document, "application/octet-stream")}
        return await _request_async("sendDocument", data, files=files)
    else:
        data["document"] = document
        return await _request_async("sendDocument", data)


async def send_media_group(chat_id, media_files, caption=None):
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
    return await _request_async("sendMediaGroup", data, files=files)


async def get_chat(chat_id):
    return await _request_async("getChat", {"chat_id": chat_id})


async def get_me():
    return await _request_async("getMe")


async def edit_message_text(chat_id, message_id, text):
    return await _request_async("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})


async def edit_message_caption(chat_id, message_id, caption):
    return await _request_async("editMessageCaption", {"chat_id": chat_id, "message_id": message_id, "caption": caption})


async def delete_message(chat_id, message_id):
    return await _request_async("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
