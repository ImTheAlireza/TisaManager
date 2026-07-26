import unittest

from utils import html_text


class HtmlTextTests(unittest.TestCase):
    def test_escapes_markup_without_changing_plain_text(self):
        self.assertEqual(html_text("hello & <world>"), "hello &amp; &lt;world&gt;")

    def test_none_is_safe(self):
        self.assertEqual(html_text(None), "")


if __name__ == "__main__":
    unittest.main()
