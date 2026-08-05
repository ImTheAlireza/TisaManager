import time
import unittest
from datetime import datetime

from utils import html_text, state_is_expired, local_utc_offset_minutes, LOCAL_TZ


class HtmlTextTests(unittest.TestCase):
    def test_escapes_markup_without_changing_plain_text(self):
        self.assertEqual(html_text("hello & <world>"), "hello &amp; &lt;world&gt;")

    def test_none_is_safe(self):
        self.assertEqual(html_text(None), "")

    def test_state_expiration(self):
        self.assertFalse(state_is_expired({"created_at": time.monotonic()}))
        self.assertTrue(state_is_expired({"created_at": time.monotonic() - 31 * 60}))


class LocalTimezoneTests(unittest.TestCase):
    def test_offset_matches_display_timezone(self):
        expected = int(datetime.now(LOCAL_TZ).utcoffset().total_seconds() // 60)
        self.assertEqual(local_utc_offset_minutes(), expected)

    def test_tehran_offset_is_fixed_plus_210(self):
        # Iran abolished DST in 2022: UTC+3:30 year-round. Guard the exact
        # offset the SQL bucketing relies on whenever Tehran is the zone.
        if str(LOCAL_TZ) == "Asia/Tehran":
            self.assertEqual(local_utc_offset_minutes(), 210)


if __name__ == "__main__":
    unittest.main()
