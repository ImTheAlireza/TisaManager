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


if __name__ == "__main__":
    unittest.main()
