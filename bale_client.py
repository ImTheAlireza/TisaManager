"""Bale API client — Telegram-compatible API at https://tapi.bale.ai

A second Bale bot can be configured (BALE_TOKEN_2) as a backup sender.
Delivery attempts alternate between the bots — attempt 1 goes out through
bot 1, attempt 2 through bot 2, attempt 3 through bot 1, and so on — so a
rate-limited or blocked bot is swapped for a fresh one on the next try.
Without BALE_TOKEN_2 every attempt uses the primary bot.

Every failure keeps the reason the API gave: the body of an HTTP 4xx/5xx is
read back and returned as Bale's own ``ok: false`` payload, so a log line
(or a delivery row in the health dashboard) says what was actually rejected
instead of a bare "HTTP Error 400: Bad Request".

Every failure also names the layer that answered, because a Bale call can be
refused in three very different places and the fix differs accordingly:

``bale``
    the API itself. Its rejections are always JSON (``{"ok": false, ...}``),
    and the bridge passes that body through untouched.
``bridge-host``
    the relay host in front of ``bale_bridge.php``: it answered with its own
    page *before* PHP ran (403 from a WAF or a suspended account, a host error
    page), so nothing ever reached Bale. Detected by the absence of the
    ``X-Bridge`` header the script adds to its own answers.
``edge``
    something else on the way (Bale's own edge, a proxy, a filter) answered
    with a non-JSON page.

A 403 that is not Bale's JSON is treated as a front-end block: it is retried
once (a WAF/rate-limit block is often intermittent, and no bot saw the request
so a retry cannot duplicate a post). When the relay host is the one refusing,
that retry does not go back into the same block: it goes out along the other
route — through ``BALE_PROXY`` if one is configured, otherwise straight to the
API, so a blocked relay does not take Bale delivery down with it.
"""

import json
import logging
import asyncio
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from config import (BALE_TOKEN, BALE_TOKEN_2, BALE_TIMEOUT, BALE_UPLOAD_TIMEOUT,
                    BALE_PROXY, BALE_API_BASE, BALE_BRIDGE_KEY,
                    BALE_ROUTE_COOLDOWN_SECONDS)

logger = logging.getLogger(__name__)

# A media group holds 2-10 items. Anything outside that is not an album: one
# item goes out as a single photo/video, a longer list is split into albums of
# ten (both Telegram and Bale reject the over-long group).
MEDIA_GROUP_MAX_ITEMS = 10


def _is_retryable_network_error(exc: Exception) -> bool:
    """Network-level failures that are safe to retry once.

    Read timeouts are excluded: by the time a *read* times out the request
    has already reached the server, so a retry could duplicate the post. A
    connect or *write* failure means the server never received the full
    request, and HTTP error responses mean it received and rejected it.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if "read operation timed out" in str(exc):
        return False
    return isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError))


def _upload_note(files) -> str:
    """Size context for a failed upload, e.g. " [3 file(s), 41.7 MB]".

    An album is one request carrying every file in it, so the size is usually
    what decides the outcome — and the failure log used to say nothing about
    what was in the request that Bale (or the bridge host) rejected.
    """
    if not files:
        return ""
    total = sum(len(blob) for (_name, blob, _ctype) in files.values()
                if isinstance(blob, (bytes, bytearray)))
    return f" [{len(files)} file(s), {total / 1048576:.1f} MB]"


# The bridge script stamps its own answers with this header
# (bridge/bale_bridge.php). It is the only reliable way to tell WHICH layer
# refused a request: the relay host's web server answers before PHP runs, so a
# response without this header never reached the bridge script — while one with
# it carries Bale's verbatim answer, passed through by the script.
BRIDGE_HEADER = "x-bridge"

# What each layer's refusal means, in the words that end up in the log line and
# in the delivery row of the health dashboard. "bale" needs no note: its JSON
# body already says why.
_ORIGIN_NOTES = {
    "bridge-host": "the bridge host refused it (the relay script never ran)",
    "edge": "refused before the Bale API (edge/front-end layer)",
}


def _response_header(exc: Exception, name: str):
    """One header of a failed response, or None when there was no response.

    Handles both the case-insensitive ``email.message.Message`` urllib really
    produces and a plain dict (tests, and other HTTP stacks): HTTP header names
    are case-insensitive, a dict lookup is not.
    """
    headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:
        return None
    if value is not None:
        return value
    try:
        for key in headers:
            if str(key).lower() == name.lower():
                return headers[key]
    except Exception:
        return None
    return None


def _read_body(exc: Exception) -> bytes:
    """The body of an HTTPError. Read ONCE — reading it consumes the stream.

    Callers that need the body twice (to decide on a retry *and* to report the
    failure) must read it here first and pass it on, or the second read comes
    back empty and the log loses the explanation.
    """
    if not isinstance(exc, urllib.error.HTTPError):
        return b""
    try:
        return exc.read() or b""
    except Exception:  # a body that cannot be read is not worth crashing over
        return b""


def _body_is_json(body) -> bool:
    """True when the response body is a JSON object (Bale's answer shape)."""
    text = (body or b"").decode("utf-8", "replace").strip()
    if not text:
        return False
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def _failure_origin(exc: Exception, body, via_bridge: bool):
    """Which layer answered a failed request (see the module docstring).

    ``None`` means no HTTP response at all: the connection failed, so the
    exception text is the only evidence (a DNS/TLS/connect problem).
    """
    if not isinstance(exc, urllib.error.HTTPError):
        return None
    if _body_is_json(body):
        return "bale"
    if via_bridge and not _response_header(exc, BRIDGE_HEADER):
        return "bridge-host"
    return "edge"


def _server_note(exc: Exception) -> str:
    """The web server that answered, when it named itself (LiteSpeed, nginx…).

    A relaying host that blocks a request usually does it from the web server
    or a WAF in front of PHP, so this is the fingerprint of the blocking layer.
    """
    server = _response_header(exc, "server")
    return f" [server: {server}]" if server else ""


def _is_front_end_block(code, body) -> bool:
    """A 403/429 that never reached the API, so a retry is safe and may work.

    Bale answers every rejection in JSON. A 403/429 whose body is *not* JSON
    came from a filter in front of it — a WAF, a rate limiter, the relay host,
    an edge rule — and those blocks are frequently intermittent. No bot saw the
    request, so re-sending cannot duplicate a post.
    """
    if code not in (403, 429):
        return False
    return not _body_is_json(body)


def _plan_retry(exc: Exception, body, via_bridge: bool):
    """Why another request is worth making — or None when it is not.

    None means the next request would only get the same answer (Bale rejected
    the request itself, or a read timeout that may already have delivered it),
    so the caller reports the failure instead of burning another attempt.

    Three failures are worth one more request:

    * the relay host refused it (its page, its WAF) — a real arrangement: a
      shared host whose BitNinja / ModSecurity rule blocks the POST while the
      bot server itself can still reach tapi.bale.ai directly,
    * a 403/429 from a front-end filter, which is often intermittent,
    * a connect/write failure, where the request never left in full.
    """
    code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
    if via_bridge and _failure_origin(exc, body, via_bridge) == "bridge-host":
        return "the bridge host refused it"
    if _is_front_end_block(code, body):
        return f"HTTP {code} from a front-end filter, so no bot saw the request"
    if _is_retryable_network_error(exc):
        return "transient network failure — the request never left in full"
    return None


def _error_result(exc: Exception, via_bridge: bool = False, body=None) -> dict:
    """Build Bale's ``ok: false`` payload out of whatever HTTP raised.

    urllib turns every 4xx/5xx into an HTTPError and *throws the body away*,
    so a rejected album, a bridge host that refused the upload and a web
    server error page all reached the log as the identical, useless
    "HTTP Error 400: Bad Request". An HTTPError is itself a readable
    response, so the explanation is read back here:

      - Bale/bridge JSON -> its ``description``/``error_code`` are kept
        verbatim ("Bad Request: group send failed", "request body ... exceeds
        this host's post_max_size (8M)"),
      - an HTML error page from the host -> a trimmed snippet, which is what
        actually identifies a 413/502 from the web server,
      - nothing readable -> the exception text, as before.

    ``via_bridge`` says whether the call went out through the Iran-side relay.
    That one flag is what makes a 403 from the relay host itself (the PHP never
    ran, nothing reached Bale) distinguishable from a 403 from Bale's edge, so
    the recorded reason names the layer to go and fix.

    ``body`` is an already-read body (a caller that read it to decide on a
    retry must pass it in, since reading an HTTPError consumes it).
    """
    result = {"ok": False}
    if isinstance(exc, urllib.error.HTTPError):
        result["error_code"] = exc.code
    if body is None:
        body = _read_body(exc)
    text = (body or b"").decode("utf-8", "replace").strip()
    payload = None
    if text:
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
    origin = _failure_origin(exc, body, via_bridge)
    description = None
    if isinstance(payload, dict):
        if payload.get("error_code") is not None:
            result["error_code"] = payload["error_code"]
        description = payload.get("description") or payload.get("error_message")
    elif text:
        # An HTML error page (a relay host that blocked the request, a WAF page,
        # a 413/502 from the web server, a cPanel page, ...) is the only
        # evidence of who refused it.
        snippet = " ".join(text.split())[:200]
        note = _ORIGIN_NOTES.get(origin)
        description = f"{note}: non-JSON response: {snippet}" if note \
            else f"non-JSON response: {snippet}"
    result["description"] = description or str(exc)
    if origin:
        result["origin"] = origin
    return result


def describe_failure(exc: Exception, via_bridge: bool = False, body=None) -> dict:
    """Public form of the failure reporter, for the doctor script and tests.

    ``body`` is for callers that already read it (reading an HTTPError twice
    yields nothing the second time).
    """
    if body is None:
        body = _read_body(exc)
    return _error_result(exc, via_bridge=via_bridge, body=body)


def _build_multipart(data=None, files=None) -> tuple:
    """Build a multipart/form-data body. Returns (content_type, body bytes).

    Kept separate from the HTTP layer so the encoding can be unit-tested —
    a missed .encode() here once crashed every Bale file upload client-side
    with 'can't concat str to bytes' before the request ever left the bot.
    The parts are joined in one pass: repeated ``body +=`` would re-copy the
    whole buffer once per file, adding seconds for large albums.
    """
    boundary = "----FormBoundary7MA4YWxkTrZu0gW"
    parts = []
    if data:
        for key, val in data.items():
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            parts.append(str(val).encode())
            parts.append(b"\r\n")
    for key, (filename, filedata, content_type) in (files or {}).items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'.encode())
        parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        parts.append(filedata)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


_PROXY_OPENER = None
_BRIDGE_LOGGED = False


# --- Route state ------------------------------------------------------------
# A Bale call has up to two routes: the Iran-side bridge, and the direct (or
# proxied) connection. One can be blocked while the other works — the deployed
# case was a BitNinja WAF answering every POST to the relay host with a 403
# page while tapi.bale.ai answered the bot server normally — and the bridge
# exists because the direct route was *not* always usable before. So the two
# are not alternatives to pick between: whichever answers is used.
#
# What a refusal costs, though, is one failed attempt per call. To avoid
# paying that on every single post, a route that answers with a block is
# skipped for BALE_ROUTE_COOLDOWN_SECONDS and the other route carries the
# calls. After the cooldown the paused route is tried again, so a fixed bridge
# takes over again on its own — no .env edit and no restart.
_route_lock = threading.Lock()
_route_paused_until: dict = {}

# Route names as they appear in log lines and in the health dashboard.
ROUTE_LABELS = {"bridge": "bridge", "direct": "direct/proxy"}


def _pause_route(route: str, reason: str, other_route_exists: bool = True) -> None:
    """Skip ``route`` for the cooldown: it is refusing requests, not the calls."""
    if BALE_ROUTE_COOLDOWN_SECONDS <= 0:
        return
    now = time.monotonic()
    with _route_lock:
        already = _route_paused_until.get(route, 0.0) > now
        _route_paused_until[route] = now + BALE_ROUTE_COOLDOWN_SECONDS
    if already:
        return
    minutes = max(1, BALE_ROUTE_COOLDOWN_SECONDS // 60)
    logger.warning(
        "Bale route '%s' is refusing requests (%s) — %s for the next %d minute(s)",
        route, reason,
        "using the other route" if other_route_exists else "still retrying it",
        minutes,
    )


def _resume_route(route: str) -> None:
    """Note that a route answered; it is back to normal if it had been paused."""
    with _route_lock:
        was_paused = _route_paused_until.pop(route, None) is not None
    if was_paused:
        logger.info("Bale route '%s' answered again — back to normal", route)


def _route_paused(route: str) -> bool:
    with _route_lock:
        until = _route_paused_until.get(route)
    return until is not None and time.monotonic() < until


def reset_route_state() -> None:
    """Forget every route pause (used by tests and by the doctor script)."""
    with _route_lock:
        _route_paused_until.clear()


def _route_order(via_bridge: bool) -> list:
    """The routes to try, in order, for the current configuration.

    The configured route goes first. A paused route goes *last* rather than
    being dropped: if the direct connection is the thing that is broken — the
    reason the bridge was set up — the relay is still worth one attempt.
    """
    if not via_bridge:
        return ["direct"]
    if _route_paused("bridge") and not _route_paused("direct"):
        return ["direct", "bridge"]
    return ["bridge", "direct"]


def _get_opener():
    """The urllib opener for Bale API calls, with the proxy applied if set.

    Built lazily and cached. With BALE_PROXY set, an explicit ProxyHandler
    routes every Bale request through that relay (an HTTP proxy tunnelling
    HTTPS with CONNECT) — needed when the bot server sits outside Iran and
    Bale's edge refuses its TCP connections. Without it, build_opener() still
    installs an env-driven ProxyHandler, so plain urlopen behaviour
    (http_proxy/https_proxy) is preserved.
    """
    global _PROXY_OPENER
    if _PROXY_OPENER is None:
        handlers = []
        if BALE_PROXY:
            proxy = BALE_PROXY if "://" in BALE_PROXY else f"http://{BALE_PROXY}"
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            logger.info("Bale client using proxy relay for tapi.bale.ai")
        _PROXY_OPENER = urllib.request.build_opener(*handlers)
    return _PROXY_OPENER


def _post_via_bridge(token, method, data=None, files=None):
    """Send one API call through the Iran-side PHP bridge (bridge/bale_bridge.php).

    The token and method travel in headers — never the URL — so they cannot
    end up in the bridge host's access logs. The body is always multipart,
    which the bridge rebuilds toward tapi.bale.ai; it answers with Bale's
    verbatim JSON.
    """
    from urllib.request import Request

    global _BRIDGE_LOGGED
    if not _BRIDGE_LOGGED:
        # Say once which transport is live. An API error read through the
        # relay can come from Bale *or* from the PHP host in front of it, and
        # the two are told apart by exactly one line in the log.
        _BRIDGE_LOGGED = True
        logger.info("Bale client using Iran-side bridge for tapi.bale.ai: %s", BALE_API_BASE)
    content_type, body = _build_multipart(data=data, files=files)
    req = Request(BALE_API_BASE, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("X-Bridge-Key", BALE_BRIDGE_KEY or "")
    req.add_header("X-Bale-Token", token)
    req.add_header("X-Bale-Method", method)
    timeout = BALE_UPLOAD_TIMEOUT if files else BALE_TIMEOUT
    resp = _get_opener().open(req, timeout=timeout)
    return json.loads(resp.read().decode())


def _post(url, data=None, files=None):
    """Synchronous HTTP POST using urllib (no external deps)."""
    from urllib.request import Request
    from urllib.parse import urlencode

    if files:
        content_type, body = _build_multipart(data=data, files=files)
        req = Request(url, data=body, method="POST")
        req.add_header("Content-Type", content_type)
        # File uploads get the long timeout: a short one kills a large video
        # the moment the connection stalls, even though it would have
        # finished seconds later.
        resp_timeout = BALE_UPLOAD_TIMEOUT
    elif data:
        encoded = urlencode(data).encode()
        req = Request(url, data=encoded, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        resp_timeout = BALE_TIMEOUT
    else:
        req = Request(url, method="POST")
        resp_timeout = BALE_TIMEOUT

    resp = _get_opener().open(req, timeout=resp_timeout)
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

    def _call(self, route, method, url, data=None, files=None):
        """One API call along one route."""
        if route == "bridge":
            return _post_via_bridge(self.token, method, data, files)
        return _post(url, data=data, files=files)

    def _report_failure(self, method, failures, files):
        """Log and return the failure that explains the outage.

        With two routes configured, both answers matter and one log line
        carries both: the first is what the configured route said (usually the
        actionable one — the WAF page and the host that sent it), and the
        rest is what the alternative answered when it was tried.
        """
        route, exc, body = failures[0]
        extra = ""
        if len(failures) > 1 and failures[-1][0] != route:
            last_route, last_exc, last_body = failures[-1]
            second = _error_result(last_exc, via_bridge=last_route == "bridge",
                                   body=last_body)
            extra = (f" (the {ROUTE_LABELS[last_route]} route also failed: "
                     f"{second['description'][:120]})")
        note = _upload_note(files) + extra
        if isinstance(exc, urllib.error.HTTPError):
            result = _error_result(exc, via_bridge=route == "bridge", body=body)
            logger.error(
                "Bale API request failed on %s (%s): HTTP %s — %s%s%s",
                method, self.name, result.get("error_code"),
                result["description"], _server_note(exc), note,
            )
            return result
        logger.error("Bale API request failed on %s (%s): %s%s%s",
                     method, self.name, exc, _server_note(exc), note)
        return {"ok": False, "description": f"{exc}{extra}"}

    def _request(self, method, data=None, files=None):
        """Run one API call, trying each configured route until one answers.

        Routes are tried in the order ``_route_order`` returns (the configured
        one first, a route that is in its cooldown last). A route with another
        route behind it gets a single attempt: the next attempt is better spent
        on the other route than on the same block. The last route is also the
        one with nowhere else to go, so it gets one retry — a front-end block
        or a reset line is often momentary.
        """
        url = f"{self.base_url}/{method}"
        routes = _route_order(bool(BALE_API_BASE))
        failures = []          # (route, exc, body) in the order they happened
        for route in routes:
            other_route = route != routes[-1]
            attempts_left = 2 if not other_route else 1
            while attempts_left:
                attempts_left -= 1
                try:
                    result = self._call(route, method, url, data, files)
                except Exception as e:
                    # Read the body now: it is needed to classify the failure
                    # and would be empty if read again later.
                    body = _read_body(e)
                    failures.append((route, e, body))
                    reason = _plan_retry(e, body, route == "bridge")
                    if reason:
                        _pause_route(route, reason, other_route_exists=other_route)
                    if not reason or not (attempts_left or other_route):
                        return self._report_failure(method, failures, files)
                    if attempts_left:
                        follow_up = "retrying the same route once"
                    else:
                        follow_up = ("trying the other route (%s)"
                                     % ROUTE_LABELS[routes[routes.index(route) + 1]])
                    logger.warning(
                        "Bale API attempt failed on %s (%s): %s — %s",
                        method, self.name, reason, follow_up,
                    )
                    time.sleep(2)
                    continue
                _resume_route(route)
                if not result.get("ok"):
                    logger.error("Bale API error on %s (%s): %s", method, self.name, result)
                else:
                    logger.info("Bale API success on %s (%s)", method, self.name)
                return result
        return self._report_failure(method, failures, files)

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
        """Send an album of photos/videos, adapting to what the API accepts.

        media_files: list of (type, file_bytes) tuples

        A media group must hold 2-10 items — Bale answers a one-item group with
        "Bad Request: malformed request" and a longer one with a 400 as well.
        The shapes that cannot be sent as one group are reshaped rather than
        dropped: a single item is what the user actually posted, so it goes out
        as one photo/video, and a longer list is split into albums of ten.
        """
        items = list(media_files or [])
        if not items:
            return {"ok": False, "description": "album has no media"}
        if len(items) == 1:
            media_type, blob = items[0]
            if media_type == "photo":
                return await self.send_photo(chat_id, blob, caption=caption)
            if media_type == "video":
                return await self.send_video(chat_id, blob, caption=caption)
            return {"ok": False, "description": f"unsupported media type: {media_type}"}
        if len(items) <= MEDIA_GROUP_MAX_ITEMS:
            return await self._send_album(chat_id, items, caption)

        messages = []
        for start in range(0, len(items), MEDIA_GROUP_MAX_ITEMS):
            chunk = items[start:start + MEDIA_GROUP_MAX_ITEMS]
            result = await self._send_album(chat_id, chunk, caption if start == 0 else None)
            if not (result and result.get("ok")):
                if not messages:
                    return result or {"ok": False, "description": "empty Bale response"}
                # The earlier albums are already in the channel. Reporting a
                # plain failure would have the 10-minute retry send the whole
                # album again and duplicate them, so the partial delivery is
                # flagged for the caller.
                reason = (result or {}).get("description", "Bale API error")
                return {
                    "ok": False,
                    "partial": True,
                    "delivered": len(messages),
                    "description": f"{len(messages)} of {len(items)} media already "
                                   f"delivered, the rest failed: {reason}",
                }
            sent = result.get("result") or []
            messages.extend(sent if isinstance(sent, list) else [sent])
        return {"ok": True, "result": messages}

    async def _send_album(self, chat_id, media_files, caption=None):
        """One sendMediaGroup request, files uploaded via attach:// syntax."""
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


def _mask_credentials(url: str) -> str:
    """``http://user:secret@host:3128`` -> ``http://user:***@host:3128``.

    The transport is printed in logs and in the health dashboard, and a proxy
    password must not travel with it.
    """
    if "@" not in url:
        return url
    scheme, sep, rest = url.partition("://")
    credentials, _, host = rest.rpartition("@")
    if ":" in credentials:
        credentials = credentials.split(":", 1)[0] + ":***"
    return f"{scheme}{sep}{credentials}@{host}" if sep else f"{credentials}@{host}"


def describe_transport() -> dict:
    """Which route Bale calls take right now, and what they fall back to.

    Used by the health dashboard and by ``diagnose_bale.py``: during an outage
    the first question is always "which path is actually in use", and the
    answer is a file that lives outside the repo (.env). ``paused`` says
    whether the configured route is currently being skipped after refusing
    requests (see _pause_route).
    """
    direct_fa = "پروکسی" if BALE_PROXY else "مستقیم"
    if BALE_API_BASE:
        paused = _route_paused("bridge")
        return {
            "route": "bridge",
            "fa": "پل ایران (bridge)",
            "active_fa": direct_fa if paused else "پل ایران (bridge)",
            "detail": BALE_API_BASE,
            "bridge_host": urlsplit(BALE_API_BASE).netloc,
            "fallback_fa": "پل ایران (bridge)" if paused else direct_fa,
            "paused": paused,
        }
    if BALE_PROXY:
        return {"route": "proxy", "fa": "پروکسی", "active_fa": "پروکسی",
                "detail": _mask_credentials(BALE_PROXY), "bridge_host": None,
                "fallback_fa": None, "paused": False}
    return {"route": "direct", "fa": "مستقیم", "active_fa": "مستقیم",
            "detail": "tapi.bale.ai", "bridge_host": None,
            "fallback_fa": None, "paused": False}


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
