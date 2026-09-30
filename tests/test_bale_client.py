"""Tests for the Bale client's request encoding.

Locks down the production bug where every file upload crashed client-side
with "can't concat str to bytes" — a header line was appended to the bytes
body without .encode(), so photos/videos/documents/media groups never
reached the Bale API at all.
"""

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("BALE_TOKEN", "test-token")
os.environ.setdefault("SUDO_USER_ID", "1")

import bale_client  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class MultipartEncodingTests(unittest.TestCase):
    def test_body_is_bytes_end_to_end(self):
        content_type, body = bale_client._build_multipart(
            data={"chat_id": 123, "media": "[{\"type\": \"photo\"}]"},
            files={"file_0": ("file_0.jpg", b"\xff\xd8fakejpeg", "image/jpeg")},
        )
        self.assertIsInstance(body, bytes, "a str fragment crashes urllib uploads")
        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(b"123", body)
        self.assertIn(b'name="file_0"; filename="file_0.jpg"', body)
        self.assertIn(b"Content-Type: image/jpeg", body)
        self.assertIn(b"\xff\xd8fakejpeg", body)
        self.assertTrue(body.endswith(b"--\r\n"), "the closing boundary must terminate the body")

    def test_data_only_upload_is_still_bytes(self):
        _, body = bale_client._build_multipart(data={"chat_id": 1}, files=None)
        self.assertIsInstance(body, bytes)


class SendMediaGroupTests(unittest.TestCase):
    """The full method path, with the HTTP layer stubbed out."""

    def test_media_group_request_is_assembled_without_typeerror(self):
        client = bale_client.BaleClient("token", "bale-test")
        captured = {}

        def fake_post(url, data=None, files=None):
            captured["url"] = url
            captured["data"] = data
            captured["files"] = files
            return {"ok": True, "result": [{"message_id": 11}, {"message_id": 12}]}

        original = bale_client._post
        bale_client._post = fake_post
        try:
            result = run(client.send_media_group(
                5033953014,
                [("photo", b"img-bytes"), ("video", b"vid-bytes")],
                caption="کپشن",
            ))
        finally:
            bale_client._post = original

        self.assertTrue(result["ok"])
        self.assertIn("sendMediaGroup", captured["url"])
        media = json.loads(captured["data"]["media"])
        self.assertEqual(media[0]["media"], "attach://file_0")
        self.assertEqual(media[1]["media"], "attach://file_1")
        self.assertEqual(media[0]["caption"], "کپشن")
        self.assertIn("file_0", captured["files"])
        self.assertIn("file_1", captured["files"])

    def test_single_photo_upload_goes_through_the_same_multipart_path(self):
        client = bale_client.BaleClient("token", "bale-test")

        def fake_post(url, data=None, files=None):
            # This used to raise TypeError: can't concat str to bytes.
            bale_client._build_multipart(data=data, files=files)
            return {"ok": True, "result": {"message_id": 7}}

        original = bale_client._post
        bale_client._post = fake_post
        try:
            result = run(client.send_photo(123, b"photo-bytes", caption="hi"))
        finally:
            bale_client._post = original

        self.assertTrue(result["ok"])


class TransientNetworkRetryTests(unittest.TestCase):
    """A flaky line gets one automatic retry — but only when it is safe."""

    def test_classifier(self):
        import urllib.error
        write_timeout = urllib.error.URLError("The write operation timed out")
        read_timeout = urllib.error.URLError("The read operation timed out")
        reset = urllib.error.URLError("Connection reset by peer")
        self.assertTrue(bale_client._is_retryable_network_error(write_timeout),
                        "the server never got the full request; retry is safe")
        self.assertTrue(bale_client._is_retryable_network_error(reset))
        self.assertFalse(bale_client._is_retryable_network_error(read_timeout),
                         "the request already reached the server; retry may duplicate")
        self.assertFalse(bale_client._is_retryable_network_error(RuntimeError("Forbidden")))
        self.assertFalse(bale_client._is_retryable_network_error(
            urllib.error.HTTPError("u", 403, "Forbidden", {}, None)))

    def _run_with(self, exc_or_result):
        client = bale_client.BaleClient("t", "bale-test")
        calls = []

        def fake_post(url, data=None, files=None):
            calls.append(1)
            if len(calls) == 1 and isinstance(exc_or_result, Exception):
                raise exc_or_result
            return {"ok": True, "result": {"message_id": 1}}

        original_post, original_sleep = bale_client._post, bale_client.time.sleep
        bale_client._post = fake_post
        bale_client.time.sleep = lambda s: None
        try:
            result = run(client.send_message(1, "hi"))
        finally:
            bale_client._post, bale_client.time.sleep = original_post, original_sleep
        return result, len(calls)

    def test_write_timeout_is_retried_once_and_recovers(self):
        import urllib.error
        result, calls = self._run_with(urllib.error.URLError("The write operation timed out"))
        self.assertTrue(result["ok"])
        self.assertEqual(calls, 2, "exactly one automatic retry")

    def test_read_timeout_is_not_retried(self):
        import urllib.error
        result, calls = self._run_with(urllib.error.URLError("The read operation timed out"))
        self.assertFalse(result["ok"])
        self.assertEqual(calls, 1, "retrying a read timeout could duplicate the post")

    def test_upload_timeout_is_longer_than_api_timeout(self):
        from config import BALE_TIMEOUT, BALE_UPLOAD_TIMEOUT
        self.assertGreater(BALE_UPLOAD_TIMEOUT, BALE_TIMEOUT,
                           "large media uploads need more headroom than getMe-style calls")


class ProxyOpenerTests(unittest.TestCase):
    """BALE_PROXY routes Bale traffic through an Iran-side relay; the opener
    must honour it and forget it between configurations."""

    def setUp(self):
        self._original = (bale_client.BALE_PROXY, bale_client._PROXY_OPENER)

    def tearDown(self):
        bale_client.BALE_PROXY, bale_client._PROXY_OPENER = self._original

    @staticmethod
    def _proxy_of(opener):
        import urllib.request
        handlers = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        return handlers[-1].proxies if handlers else {}

    def test_explicit_proxy_is_applied(self):
        bale_client.BALE_PROXY = "http://user:pass@10.0.0.5:3128"
        bale_client._PROXY_OPENER = None
        proxies = self._proxy_of(bale_client._get_opener())
        self.assertEqual(proxies.get("https"), "http://user:pass@10.0.0.5:3128")
        self.assertEqual(proxies.get("http"), "http://user:pass@10.0.0.5:3128")

    def test_schemeless_proxy_gets_http_scheme(self):
        bale_client.BALE_PROXY = "10.0.0.5:3128"
        bale_client._PROXY_OPENER = None
        proxies = self._proxy_of(bale_client._get_opener())
        self.assertEqual(proxies.get("https"), "http://10.0.0.5:3128")

    def test_unset_proxy_forces_no_relay(self):
        bale_client.BALE_PROXY = None
        bale_client._PROXY_OPENER = None
        proxies = self._proxy_of(bale_client._get_opener())
        self.assertIsNone(proxies.get("https"),
                          "without BALE_PROXY the opener must not gain a forced relay")


class BridgeTests(unittest.TestCase):
    """BALE_API_BASE mode: token/method go in headers, never the URL."""

    def setUp(self):
        self._original = (bale_client.BALE_API_BASE, bale_client.BALE_BRIDGE_KEY,
                          bale_client._PROXY_OPENER, bale_client._post_via_bridge,
                          bale_client._get_opener)
        bale_client.BALE_API_BASE = "https://site.example/bale_bridge.php"
        bale_client.BALE_BRIDGE_KEY = "secret"
        bale_client._PROXY_OPENER = None

    def tearDown(self):
        (bale_client.BALE_API_BASE, bale_client.BALE_BRIDGE_KEY,
         bale_client._PROXY_OPENER, bale_client._post_via_bridge,
         bale_client._get_opener) = self._original

    def test_request_is_sent_with_token_and_method_in_headers(self):
        captured = {}

        class FakeResponse:
            def read(self):
                return b'{"ok": true, "result": {"message_id": 7}}'

        class FakeOpener:
            def open(self, req, timeout=None):
                captured["url"] = req.full_url
                captured["headers"] = {k.lower(): v for k, v in req.header_items()}
                captured["timeout"] = timeout
                captured["body"] = req.data
                return FakeResponse()

        bale_client._get_opener = lambda: FakeOpener()
        result = bale_client.BaleClient("123:abc", "bale-1")._request("sendMessage", {"chat_id": 5, "text": "hi"})

        self.assertTrue(result["ok"])
        self.assertEqual(captured["url"], "https://site.example/bale_bridge.php")
        self.assertEqual(captured["headers"]["x-bale-token"], "123:abc",
                         "the token must ride in a header, not the URL")
        self.assertEqual(captured["headers"]["x-bale-method"], "sendMessage")
        self.assertEqual(captured["headers"]["x-bridge-key"], "secret")
        self.assertIn(b'name="chat_id"', captured["body"])

    def test_bridge_failure_gives_ok_false(self):
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.URLError("connection refused")

        bale_client._post_via_bridge = boom
        result = bale_client.BaleClient("123:abc", "bale-1")._request("getMe")
        self.assertFalse(result["ok"])


def _http_error(code, body: bytes, reason="Bad Request"):
    """An HTTPError that carries a response body, the way urllib raises one."""
    import io
    import urllib.error

    return urllib.error.HTTPError(
        "https://tapi.bale.ai/bot1/sendMediaGroup", code, reason, {}, io.BytesIO(body)
    )


class HttpErrorReportingTests(unittest.TestCase):
    """A rejected request must say WHY.

    urllib discards the body of every 4xx, so an album that Bale refused, a
    bridge host that dropped an oversized upload and a web server error page
    all reached the log as the same "HTTP Error 400: Bad Request" — the
    production failure this covers.
    """

    def setUp(self):
        self._original = (bale_client.BALE_API_BASE, bale_client._post)

    def tearDown(self):
        bale_client.BALE_API_BASE, bale_client._post = self._original

    def test_bale_description_is_kept(self):
        result = bale_client._error_result(_http_error(
            400, b'{"ok": false, "error_code": 400, '
                b'"description": "Bad Request: group send failed"}'))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], 400)
        self.assertEqual(result["description"], "Bad Request: group send failed")

    def test_bridge_json_error_is_kept(self):
        result = bale_client._error_result(_http_error(
            413, b'{"ok": false, "error_code": 413, "description": '
                b'"request body of 41.7 MB exceeds this host\'s post_max_size (8M)."}'))

        self.assertIn("post_max_size (8M)", result["description"])
        self.assertEqual(result["error_code"], 413)

    def test_html_error_page_is_kept_as_a_snippet(self):
        html = b"<html><body><h1>413 Request Entity Too Large</h1></body></html>"
        result = bale_client._error_result(_http_error(413, html))

        self.assertIn("non-JSON response", result["description"])
        self.assertIn("413 Request Entity Too Large", result["description"])

    def test_empty_body_falls_back_to_the_exception_text(self):
        result = bale_client._error_result(_http_error(400, b""))
        self.assertEqual(result["description"], "HTTP Error 400: Bad Request")
        self.assertEqual(result["error_code"], 400)

    def test_upload_note_reports_file_count_and_size(self):
        note = bale_client._upload_note({
            "file_0": ("file_0.jpg", b"x" * 1048576, "image/jpeg"),
            "file_1": ("file_1.mp4", b"y" * 1048576, "video/mp4"),
        })
        self.assertEqual(note, " [2 file(s), 2.0 MB]")
        self.assertEqual(bale_client._upload_note(None), "")

    def _failed_send(self, error, media_files):
        bale_client.BALE_API_BASE = None

        def fake_post(url, data=None, files=None):
            raise error

        bale_client._post = fake_post
        client = bale_client.BaleClient("token", "bale-1")
        with self.assertLogs("bale_client", level="ERROR") as logs:
            result = run(client.send_media_group(5033953014, media_files, caption="کپشن"))
        return result, "\n".join(logs.output)

    def test_failed_album_logs_the_api_reason_and_the_payload_size(self):
        result, log = self._failed_send(
            _http_error(400, b'{"ok": false, "error_code": 400, '
                           b'"description": "Bad Request: group send failed"}'),
            [("photo", b"x" * 1048576), ("video", b"y" * 1048576)],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["description"], "Bad Request: group send failed",
                         "the delivery row must store the reason, not the urllib text")
        self.assertIn("sendMediaGroup", log)
        self.assertIn("HTTP 400", log)
        self.assertIn("group send failed", log)
        self.assertIn("2 file(s), 2.0 MB", log,
                      "a rejected album needs its payload size in the same line")

    def test_failed_album_keeps_the_host_error_page(self):
        _, log = self._failed_send(
            _http_error(413, b"<h1>413 Request Entity Too Large</h1>"),
            [("video", b"z" * 1024)],
        )
        self.assertIn("413 Request Entity Too Large", log)


class BridgePhpLimitsTests(unittest.TestCase):
    """The PHP relay must not turn a host limit into a mystery 400.

    PHP silently discards a POST body over post_max_size and a file over
    upload_max_filesize; the bridge used to forward the resulting empty
    request to Bale, so an oversized album only ever produced "400 Bad
    Request" in the bot log. These checks lock the guard in place.
    """

    SOURCE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bridge", "bale_bridge.php",
    )

    @classmethod
    def setUpClass(cls):
        with open(cls.SOURCE, encoding="utf-8") as fh:
            cls.php = fh.read()

    def test_oversized_body_is_rejected_with_the_limit_named(self):
        self.assertIn("ini_get('post_max_size')", self.php)
        self.assertIn("$contentLength > $postMaxBytes", self.php)
        self.assertIn("fail(413", self.php)

    def test_a_body_php_dropped_is_caught_after_parsing(self):
        self.assertIn("!$post && $contentLength >= DROPPED_BODY_MIN", self.php)

    def test_upload_failures_explain_themselves(self):
        self.assertIn("upload_error_text", self.php)
        self.assertIn("upload_max_filesize", self.php)

    def test_bridge_errors_carry_an_error_code(self):
        self.assertIn("'error_code' => $code", self.php,
                      "the bot reads bridge failures like Bale ones")

    def test_setup_notes_state_the_required_ini_values(self):
        for setting in ("post_max_size = 64M", "upload_max_filesize = 50M"):
            self.assertIn(setting, self.php)

    def test_php_is_syntactically_valid(self):
        import shutil
        import subprocess

        php = shutil.which("php")
        if not php:
            self.skipTest("php CLI not installed")
        proc = subprocess.run([php, "-l", self.SOURCE], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
