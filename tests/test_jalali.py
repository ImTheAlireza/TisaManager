"""Tests for the Jalali (Solar Hijri) calendar support.

The conversion is implemented in-house (the project avoids third-party
dependencies), so the important test here is the exhaustive cross-check in
``ReferenceCrossCheckTests``: every day over a long range is compared against
the ``jdatetime`` reference library when it is installed.
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("SUDO_USER_ID", "1")

import jalali  # noqa: E402


class ConversionTests(unittest.TestCase):
    """Known-good anchor dates."""

    KNOWN = [
        # (gregorian, jalali)
        ((2024, 3, 20), (1403, 1, 1)),    # Nowruz 1403
        ((2025, 3, 21), (1404, 1, 1)),    # Nowruz 1404
        ((2026, 3, 21), (1405, 1, 1)),    # Nowruz 1405
        ((2026, 8, 3), (1405, 5, 12)),    # today, when this was written
        ((1979, 2, 11), (1357, 11, 22)),  # revolution day
        ((2000, 1, 1), (1378, 10, 11)),
        ((2016, 3, 20), (1395, 1, 1)),
    ]

    def test_gregorian_to_jalali_known_dates(self):
        for g, j in self.KNOWN:
            with self.subTest(gregorian=g):
                self.assertEqual(jalali.gregorian_to_jalali(*g), j)

    def test_jalali_to_gregorian_known_dates(self):
        for g, j in self.KNOWN:
            with self.subTest(jalali=j):
                self.assertEqual(jalali.jalali_to_gregorian(*j), g)

    def test_roundtrip_is_stable(self):
        day = date(2020, 1, 1)
        for _ in range(4000):
            self.assertEqual(jalali.from_jalali(*jalali.to_jalali(day)), day)
            day += timedelta(days=1)

    def test_nowruz_is_always_farvardin_first(self):
        # Whatever Gregorian day it lands on, Nowruz is 1/1.
        for jy in range(1390, 1460):
            g = jalali.from_jalali(jy, 1, 1)
            self.assertEqual(jalali.to_jalali(g), (jy, 1, 1))


class LeapYearTests(unittest.TestCase):
    def test_known_leap_years(self):
        for jy in (1399, 1403, 1408, 1412):
            with self.subTest(year=jy):
                self.assertTrue(jalali.is_leap_jalali(jy))

    def test_known_common_years(self):
        for jy in (1400, 1401, 1402, 1404, 1405):
            with self.subTest(year=jy):
                self.assertFalse(jalali.is_leap_jalali(jy))

    def test_esfand_length_follows_leap_year(self):
        self.assertEqual(jalali.days_in_jalali_month(1403, 12), 30)
        self.assertEqual(jalali.days_in_jalali_month(1404, 12), 29)

    def test_month_lengths(self):
        for jm in range(1, 7):
            self.assertEqual(jalali.days_in_jalali_month(1404, jm), 31)
        for jm in range(7, 12):
            self.assertEqual(jalali.days_in_jalali_month(1404, jm), 30)

    def test_leap_day_exists_and_rolls_over(self):
        leap = jalali.from_jalali(1403, 12, 30)
        self.assertEqual(jalali.to_jalali(leap + timedelta(days=1)), (1404, 1, 1))

    def test_invalid_leap_day_is_rejected(self):
        with self.assertRaises(ValueError):
            jalali.from_jalali(1404, 12, 30)  # 1404 is not a leap year


class DigitTests(unittest.TestCase):
    def test_to_persian_digits(self):
        self.assertEqual(jalali.to_persian_digits("1405-05-12"), "۱۴۰۵-۰۵-۱۲")

    def test_persian_digits_round_trip(self):
        self.assertEqual(jalali.to_latin_digits("۱۴۰۵-۰۵-۱۲"), "1405-05-12")

    def test_arabic_indic_digits_are_accepted(self):
        # Some keyboards emit U+0660..U+0669 rather than U+06F0..U+06F9.
        self.assertEqual(jalali.to_latin_digits("١٤٠٥"), "1405")


class ParsingTests(unittest.TestCase):
    def test_parses_dashed_jalali(self):
        self.assertEqual(jalali.parse_jalali_date("1405-05-12"), date(2026, 8, 3))

    def test_parses_slashed_jalali(self):
        self.assertEqual(jalali.parse_jalali_date("1405/5/12"), date(2026, 8, 3))

    def test_parses_persian_digits(self):
        self.assertEqual(jalali.parse_jalali_date("۱۴۰۵/۰۵/۱۲"), date(2026, 8, 3))

    def test_rejects_two_digit_year(self):
        # Guessing the century would be a good way to schedule to the wrong day.
        with self.assertRaises(ValueError):
            jalali.parse_jalali_date("05-05-12")

    def test_rejects_impossible_month(self):
        with self.assertRaises(ValueError):
            jalali.parse_jalali_date("1405-13-01")

    def test_rejects_day_beyond_month_length(self):
        with self.assertRaises(ValueError):
            jalali.parse_jalali_date("1405-07-31")  # Mehr has 30 days

    def test_rejects_garbage(self):
        for bad in ("", "abc", "1405", "1405-05", "x-y-z"):
            with self.subTest(text=bad):
                with self.assertRaises(ValueError):
                    jalali.parse_jalali_date(bad)


class FormattingTests(unittest.TestCase):
    def test_short_date(self):
        self.assertEqual(jalali.format_jalali_date(date(2026, 8, 3)), "۱۴۰۵-۰۵-۱۲")

    def test_long_date_uses_month_name(self):
        self.assertEqual(jalali.format_jalali_long(date(2026, 8, 3)), "۱۲ مرداد ۱۴۰۵")

    def test_datetime_includes_clock(self):
        self.assertEqual(
            jalali.format_jalali_datetime(datetime(2026, 8, 3, 14, 30)),
            "۱۴۰۵-۰۵-۱۲ ۱۴:۳۰",
        )

    def test_latin_digit_mode(self):
        self.assertEqual(
            jalali.format_jalali_date(date(2026, 8, 3), persian_digits=False),
            "1405-05-12",
        )


class UtilsIntegrationTests(unittest.TestCase):
    """The display helpers must localise without changing storage."""

    def setUp(self):
        import utils
        self.utils = utils
        self._orig = utils.USE_JALALI

    def tearDown(self):
        self.utils.USE_JALALI = self._orig

    def test_format_local_uses_jalali_when_enabled(self):
        self.utils.USE_JALALI = True
        # 2026-08-03 10:00 UTC -> 13:30 Tehran -> 1405-05-12
        out = self.utils.format_local(datetime(2026, 8, 3, 10, 0))
        self.assertIn("۱۴۰۵", out)

    def test_format_local_falls_back_to_gregorian(self):
        self.utils.USE_JALALI = False
        out = self.utils.format_local(datetime(2026, 8, 3, 10, 0))
        self.assertIn("2026-08-03", out)

    def test_parse_user_datetime_accepts_jalali(self):
        parsed = self.utils.parse_user_datetime("1405-05-12 14:30")
        self.assertEqual(parsed, datetime(2026, 8, 3, 14, 30))

    def test_parse_user_datetime_accepts_persian_digits(self):
        parsed = self.utils.parse_user_datetime("۱۴۰۵-۰۵-۱۲ ۱۴:۳۰")
        self.assertEqual(parsed, datetime(2026, 8, 3, 14, 30))

    def test_parse_user_datetime_still_accepts_gregorian(self):
        # A year >= 1700 is unambiguously Gregorian, so old habits keep working.
        parsed = self.utils.parse_user_datetime("2026-08-03 14:30")
        self.assertEqual(parsed, datetime(2026, 8, 3, 14, 30))

    def test_parse_rejects_missing_time(self):
        with self.assertRaises(ValueError):
            self.utils.parse_user_datetime("1405-05-12")

    def test_parse_rejects_bad_clock(self):
        for bad in ("1405-05-12 25:00", "1405-05-12 12:99", "1405-05-12 aa:bb"):
            with self.subTest(text=bad):
                with self.assertRaises(ValueError):
                    self.utils.parse_user_datetime(bad)

    def test_storage_conversion_is_unaffected_by_calendar(self):
        # The whole point: display changes, stored UTC does not.
        local = datetime(2026, 8, 3, 14, 30)
        self.utils.USE_JALALI = True
        a = self.utils.local_to_utc_naive(local)
        self.utils.USE_JALALI = False
        b = self.utils.local_to_utc_naive(local)
        self.assertEqual(a, b)
        self.assertIsNone(a.tzinfo)


class CalendarKeyboardTests(unittest.TestCase):
    def setUp(self):
        try:
            import keyboards
        except ImportError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"python-telegram-bot required: {exc}") from exc
        self.keyboards = keyboards

    def test_calendar_has_weekday_header_and_days(self):
        today = date(2026, 8, 3)          # 1405-05-12
        markup = self.keyboards.schedule_calendar_keyboard(1405, 5, today)
        header = [b.text for b in markup.inline_keyboard[0]]
        self.assertEqual(header, ["ش", "ی", "د", "س", "چ", "پ", "ج"])
        payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("schedule_date_2026-08-03", payloads)

    def test_past_days_are_not_selectable(self):
        today = date(2026, 8, 3)
        markup = self.keyboards.schedule_calendar_keyboard(1405, 5, today)
        payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
        # 1405-05-11 is yesterday and must not be tappable.
        self.assertNotIn("schedule_date_2026-08-02", payloads)

    def test_fully_past_month_has_no_prev_button(self):
        today = date(2026, 8, 3)
        markup = self.keyboards.schedule_calendar_keyboard(1405, 5, today)
        payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
        # Mordad is the current month, so Tir (entirely past) must not be offered.
        self.assertNotIn("schedule_cal_1405_4", payloads)
        self.assertIn("schedule_cal_1405_6", payloads)

    def test_month_grid_covers_every_day(self):
        today = date(2020, 1, 1)  # far in the past, so nothing is filtered
        for jy, jm in ((1403, 12), (1404, 12), (1405, 1), (1405, 7)):
            with self.subTest(year=jy, month=jm):
                markup = self.keyboards.schedule_calendar_keyboard(jy, jm, today)
                payloads = [b.callback_data for row in markup.inline_keyboard
                            for b in row if b.callback_data.startswith("schedule_date_")]
                self.assertEqual(len(payloads), jalali.days_in_jalali_month(jy, jm))

    def test_date_shortcut_keyboard_offers_calendar(self):
        markup = self.keyboards.schedule_date_keyboard(date(2026, 8, 3))
        payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("schedule_cal", payloads)

    def test_shortcut_payloads_remain_gregorian_iso(self):
        # Routing and storage must not change with the display calendar.
        markup = self.keyboards.schedule_date_keyboard(date(2026, 8, 3))
        first = markup.inline_keyboard[0][0].callback_data
        self.assertEqual(first, "schedule_date_2026-08-03")


class ReferenceCrossCheckTests(unittest.TestCase):
    """Compare every day against jdatetime, if it is available."""

    def setUp(self):
        try:
            import jdatetime
        except ImportError:  # pragma: no cover - optional dev dependency
            raise unittest.SkipTest("jdatetime not installed")
        self.jdatetime = jdatetime

    def test_every_day_matches_reference(self):
        day = date(1990, 1, 1)
        end = date(2100, 12, 31)
        checked = 0
        while day <= end:
            ref = self.jdatetime.date.fromgregorian(date=day)
            mine = jalali.to_jalali(day)
            if mine != (ref.year, ref.month, ref.day):
                self.fail(f"{day} -> {mine}, reference says "
                          f"{(ref.year, ref.month, ref.day)}")
            if jalali.from_jalali(*mine) != day:
                self.fail(f"round trip failed for {day}")
            checked += 1
            day += timedelta(days=1)
        self.assertGreater(checked, 40000)

    def test_leap_years_match_reference(self):
        for jy in range(1300, 1500):
            with self.subTest(year=jy):
                self.assertEqual(
                    jalali.is_leap_jalali(jy),
                    self.jdatetime.date(jy, 1, 1).isleap(),
                )


if __name__ == "__main__":
    unittest.main()


class CalendarAlignmentTests(unittest.TestCase):
    """Every rendered day must sit in the column of its real weekday.

    An off-by-one here silently schedules posts to the wrong day, so it is
    checked across many months rather than spot-checked.
    """

    def setUp(self):
        try:
            import keyboards
        except ImportError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"python-telegram-bot required: {exc}") from exc
        self.keyboards = keyboards
        self.past = date(1990, 1, 1)  # so nothing is filtered as past

    def test_weekday_columns_align(self):
        checked = 0
        for jy in range(1400, 1412):
            for jm in range(1, 13):
                markup = self.keyboards.schedule_calendar_keyboard(jy, jm, self.past)
                for row in markup.inline_keyboard[1:]:
                    if len(row) != 7:
                        continue
                    for col, button in enumerate(row):
                        if not button.callback_data.startswith("schedule_date_"):
                            continue
                        g = date.fromisoformat(
                            button.callback_data.removeprefix("schedule_date_"))
                        expected = self.keyboards._SATURDAY_FIRST.index(g.weekday())
                        self.assertEqual(
                            col, expected,
                            f"{g} rendered in column {col}, expected {expected}")
                        checked += 1
        self.assertGreater(checked, 4000)

    def test_each_month_renders_exactly_its_days(self):
        for jy in range(1400, 1412):
            for jm in range(1, 13):
                with self.subTest(year=jy, month=jm):
                    markup = self.keyboards.schedule_calendar_keyboard(jy, jm, self.past)
                    days = []
                    for row in markup.inline_keyboard[1:]:
                        for button in row:
                            if button.callback_data.startswith("schedule_date_"):
                                g = date.fromisoformat(
                                    button.callback_data.removeprefix("schedule_date_"))
                                days.append(jalali.to_jalali(g))
                    expected = [(jy, jm, d) for d in
                                range(1, jalali.days_in_jalali_month(jy, jm) + 1)]
                    self.assertEqual(days, expected)


class CalendarToggleTests(unittest.TestCase):
    """The owner-facing toggle must change display only, never storage."""

    def setUp(self):
        import utils
        self.utils = utils
        self._orig = utils.USE_JALALI

    def tearDown(self):
        self.utils.set_calendar(self._orig)

    def test_toggle_switches_rendering(self):
        stamp = datetime(2026, 8, 3, 10, 0)
        self.utils.set_calendar(True)
        self.assertIn("۱۴۰۵", self.utils.format_local(stamp))
        self.utils.set_calendar(False)
        self.assertIn("2026", self.utils.format_local(stamp))

    def test_parser_accepts_both_calendars_in_either_mode(self):
        for mode in (True, False):
            with self.subTest(jalali=mode):
                self.utils.set_calendar(mode)
                self.assertEqual(
                    self.utils.parse_user_datetime("1405-05-12 09:15"),
                    datetime(2026, 8, 3, 9, 15),
                )
                self.assertEqual(
                    self.utils.parse_user_datetime("2026-08-03 09:15"),
                    datetime(2026, 8, 3, 9, 15),
                )

    def test_date_example_matches_active_calendar(self):
        self.utils.set_calendar(True)
        self.assertTrue(self.utils.date_example().startswith("14"))
        self.utils.set_calendar(False)
        self.assertTrue(self.utils.date_example().startswith("20"))

    def test_example_is_parseable_in_both_modes(self):
        # The example we show users must actually be accepted by the parser.
        for mode in (True, False):
            with self.subTest(jalali=mode):
                self.utils.set_calendar(mode)
                self.utils.parse_user_datetime(self.utils.date_example())


class StoredTimeRenderingTests(unittest.TestCase):
    """Regression: history showed the raw DB value instead of local time.

    A post created at 14:13 UTC is 17:43 in Tehran. Rendering the stored value
    without conversion showed the DB server's wall clock instead — 95 minutes
    off on a UTC+2 server, 3.5 hours off on a Tehran one.
    """

    STORED_UTC = datetime(2026, 8, 3, 14, 13)
    EXPECTED_LOCAL_CLOCK = "۱۷:۴۳"

    def setUp(self):
        import utils
        self.utils = utils
        self._orig = utils.USE_JALALI
        utils.set_calendar(True)

    def tearDown(self):
        self.utils.set_calendar(self._orig)

    def test_detail_view_converts_to_local(self):
        out = self.utils.format_local(self.STORED_UTC)
        self.assertIn(self.EXPECTED_LOCAL_CLOCK, out)
        self.assertEqual(out, "۱۴۰۵-۰۵-۱۲ ۱۷:۴۳")

    def test_list_row_converts_to_local(self):
        out = self.utils.format_local_short(self.STORED_UTC)
        self.assertIn(self.EXPECTED_LOCAL_CLOCK, out)

    def test_clock_is_never_left_in_latin_digits(self):
        # The original bug rendered a Persian date beside a Latin clock.
        for text in (self.utils.format_local(self.STORED_UTC),
                     self.utils.format_local_short(self.STORED_UTC)):
            with self.subTest(text=text):
                self.assertFalse(any(ch in text for ch in "0123456789"),
                                 f"Latin digits leaked into {text!r}")

    def test_raw_value_is_not_what_gets_shown(self):
        # Guards against a future regression back to strftime on the raw value.
        raw = f"{self.STORED_UTC:%H:%M}"
        self.assertNotIn(raw, self.utils.format_local(self.STORED_UTC))

    def test_gregorian_mode_also_converts(self):
        self.utils.set_calendar(False)
        self.assertEqual(self.utils.format_local(self.STORED_UTC), "2026-08-03 17:43")
        self.assertEqual(self.utils.format_local_short(self.STORED_UTC), "08/03 17:43")

    def test_none_is_handled(self):
        self.assertEqual(self.utils.format_local(None), "—")
        self.assertEqual(self.utils.format_local_short(None), "")

    def test_history_row_and_detail_agree(self):
        detail = self.utils.format_local(self.STORED_UTC)
        row = self.utils.format_local_short(self.STORED_UTC)
        self.assertTrue(detail.endswith(row.split()[-1]),
                        "list and detail views disagree about the time")


class TimestampStorageSemanticsTests(unittest.TestCase):
    """MySQL TIMESTAMP is a UTC epoch converted via the SESSION timezone.

    Because MySQL converts on write *and* on read, the stored instant was
    always correct; the bug was that the code treated the server-local value
    it read back as UTC. Pinning sessions to UTC therefore fixes historical
    rows on its own.

    An earlier attempt added a DATE_ADD "rebase" migration. That was wrong: it
    shifts an already-correct epoch and corrupts the data. These tests encode
    the reasoning so it does not get reintroduced.
    """

    SERVER_OFFSET_HOURS = 2                  # e.g. a CEST database server
    TRUE_UTC = datetime(2026, 8, 3, 9, 7)    # == 12:37 Tehran

    def setUp(self):
        import utils
        self.utils = utils
        self._orig = utils.USE_JALALI
        utils.set_calendar(True)

    def tearDown(self):
        self.utils.set_calendar(self._orig)

    def _read_with_session_offset(self, hours):
        """What MySQL returns for a TIMESTAMP under a given session timezone."""
        return self.TRUE_UTC + timedelta(hours=hours)

    def test_utc_pinned_session_reads_the_true_instant(self):
        read = self._read_with_session_offset(0)
        self.assertEqual(read, self.TRUE_UTC)
        self.assertEqual(self.utils.format_local(read), "۱۴۰۵-۰۵-۱۲ ۱۲:۳۷")

    def test_unpinned_session_reproduces_the_reported_bug(self):
        read = self._read_with_session_offset(self.SERVER_OFFSET_HOURS)
        self.assertEqual(self.utils.format_local(read), "۱۴۰۵-۰۵-۱۲ ۱۴:۳۷")

    def test_rebasing_a_timestamp_column_would_corrupt_it(self):
        correct = self._read_with_session_offset(0)
        rebased = correct - timedelta(hours=self.SERVER_OFFSET_HOURS)
        self.assertNotEqual(rebased, self.TRUE_UTC)
        self.assertEqual(self.utils.format_local(rebased), "۱۴۰۵-۰۵-۱۲ ۱۰:۳۷")

    def test_no_rebase_migration_remains_in_the_schema(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        source = open(os.path.join(root, "database.py"), encoding="utf-8").read()
        self.assertNotIn("timestamps_utc_migrated", source,
                         "the corrupting rebase migration was reintroduced")
        self.assertIn("SET time_zone = '+00:00'", source,
                      "the UTC session pin is missing")

    def test_datetime_columns_are_written_as_explicit_utc(self):
        # DATETIME gets no implicit conversion, so those writes must be UTC.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        source = open(os.path.join(root, "database.py"), encoding="utf-8").read()
        self.assertIn("run_at <= UTC_TIMESTAMP()", source)
        self.assertIn("claimed_at = UTC_TIMESTAMP()", source)

    def test_session_pin_is_verified_at_startup(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        source = open(os.path.join(root, "database.py"), encoding="utf-8").read()
        self.assertIn("_assert_session_is_utc", source,
                      "startup must fail loudly if sessions are not on UTC")
