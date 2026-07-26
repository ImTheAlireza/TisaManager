import time
import unittest

from utils import html_text, state_is_expired


class HtmlTextTests(unittest.TestCase):
    def test_escapes_markup_without_changing_plain_text(self):
        self.assertEqual(html_text("hello & <world>"), "hello &amp; &lt;world&gt;")

    def test_none_is_safe(self):
        self.assertEqual(html_text(None), "")

    def test_state_expiration(self):
        self.assertFalse(state_is_expired({"created_at": time.monotonic()}))
        self.assertTrue(state_is_expired({"created_at": time.monotonic() - 31 * 60}))


if __name__ == "__main__":
    unittest.main()
