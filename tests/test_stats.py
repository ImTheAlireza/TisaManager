"""Tests for the analytics screens.

Focus is on the rendering logic that is easy to get subtly wrong: bars that
overstate success, sparklines that misrepresent quiet periods, percentages
that divide by zero, and messages that exceed Telegram's size limit.
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("SUDO_USER_ID", "1")

try:
    import handlers.stats as stats
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"python-telegram-bot required: {exc}") from exc


class BarTests(unittest.TestCase):
    def test_full_only_when_complete(self):
        # A 96% success rate must not render as a full bar; that reads as
        # "everything is fine" when 16 destinations actually failed.
        self.assertNotEqual(stats.bar(404, 420), "█" * 10)
        self.assertEqual(stats.bar(420, 420), "█" * 10)

    def test_empty_when_zero(self):
        self.assertEqual(stats.bar(0, 420), "░" * 10)

    def test_zero_total_does_not_divide_by_zero(self):
        self.assertEqual(stats.bar(0, 0), "░" * 10)

    def test_width_is_respected(self):
        for width in (4, 6, 8, 10, 20):
            with self.subTest(width=width):
                self.assertEqual(len(stats.bar(3, 7, width)), width)

    def test_monotonic(self):
        prev = -1
        for value in range(0, 21):
            filled = stats.bar(value, 20).count("█")
            self.assertGreaterEqual(filled, prev)
            prev = filled

    def test_over_full_is_clamped(self):
        # Defensive: a retry could in principle report more than attempted.
        self.assertEqual(stats.bar(11, 10), "█" * 10)


class SparklineTests(unittest.TestCase):
    def test_length_matches_input(self):
        self.assertEqual(len(stats.sparkline([1, 2, 3, 4, 5])), 5)

    def test_empty_series(self):
        self.assertEqual(stats.sparkline([]), "")

    def test_all_zero_is_flat_not_spiky(self):
        out = stats.sparkline([0, 0, 0, 0])
        self.assertEqual(out, "▁▁▁▁")

    def test_zero_is_visually_distinct_from_one(self):
        # A quiet day and a one-post day must not look the same.
        out = stats.sparkline([0, 1])
        self.assertNotEqual(out[0], out[1])

    def test_peak_reaches_top_block(self):
        self.assertTrue(stats.sparkline([1, 5, 10]).endswith("█"))

    def test_single_value(self):
        self.assertEqual(len(stats.sparkline([7])), 1)


class PercentTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(stats.pct(50, 100), "۵۰٪")

    def test_zero_total_is_dash(self):
        self.assertEqual(stats.pct(0, 0), "—")

    def test_uses_persian_digits(self):
        self.assertFalse(any(c.isdigit() and c in "0123456789"
                             for c in stats.pct(75, 100)))

    def test_handles_none(self):
        self.assertEqual(stats.pct(None, None), "—")
        self.assertEqual(stats.pct(None, 10), "۰٪")


class DeltaTests(unittest.TestCase):
    def test_increase_decrease_flat(self):
        self.assertIn("+", stats._delta(10, 5))
        self.assertIn("−", stats._delta(5, 10))
        self.assertEqual(stats._delta(5, 5), "")

    def test_persian_digits(self):
        self.assertEqual(stats._delta(10, 5), " (+۵)")


class ClipTests(unittest.TestCase):
    """Telegram rejects messages over 4096 characters."""

    def test_short_text_untouched(self):
        self.assertEqual(stats.clip("hello"), "hello")

    def test_long_text_is_clipped(self):
        out = stats.clip("\n".join(f"line {i}" for i in range(2000)))
        self.assertLessEqual(len(out), stats.TELEGRAM_LIMIT)
        self.assertIn("کوتاه شد", out)

    def test_clips_on_a_line_boundary(self):
        out = stats.clip("\n".join("x" * 50 for _ in range(500)))
        body = out.split("\n\n<i>")[0]
        self.assertFalse(body.endswith("x" * 3 + "…"))


class SummaryTests(unittest.TestCase):
    def _data(self, **over):
        base = {
            "posts": {"total": 142, "completed": 130, "partial": 5, "failed": 2,
                      "drafts": 3, "scheduled": 2, "approvals": 1},
            "windows": {"last_24h": 6, "prev_24h": 4, "last_7d": 31,
                        "prev_7d": 24, "last_30d": 118},
            "types": [{"post_type": "photo", "total": 70}],
            "pending_retries": 1,
            "channels": {"total": 4, "active": 4},
            "platforms": [{"platform": "telegram", "total": 3}],
            "deliveries": {"attempted": 420, "delivered": 404, "failing": 16},
        }
        base.update(over)
        return base

    def test_renders_windows_with_deltas(self):
        out = stats.summary_text(self._data(), True)
        self.assertIn("۶", out)
        self.assertIn("(+۲)", out)

    def test_alerts_section_appears(self):
        out = stats.summary_text(self._data(), True)
        self.assertIn("نیازمند توجه", out)

    def test_no_alerts_when_clean(self):
        out = stats.summary_text(self._data(
            pending_retries=0,
            posts={"total": 10, "completed": 10, "partial": 0, "failed": 0,
                   "drafts": 0, "scheduled": 0, "approvals": 0},
            deliveries={"attempted": 10, "delivered": 10, "failing": 0},
        ), True)
        self.assertNotIn("نیازمند توجه", out)

    def test_writer_view_is_marked_and_hides_channels(self):
        out = stats.summary_text(self._data(), False)
        self.assertIn("فقط پست‌های شما", out)
        self.assertNotIn("پلتفرم", out)

    def test_empty_database_does_not_crash(self):
        out = stats.summary_text({
            "posts": {}, "windows": {}, "types": [], "deliveries": {},
            "channels": {}, "platforms": [], "pending_retries": 0,
        }, True)
        self.assertIn("خلاصه عملکرد", out)

    def test_fits_telegram_limit(self):
        self.assertLessEqual(len(stats.summary_text(self._data(), True)),
                             stats.TELEGRAM_LIMIT)


class TrendTests(unittest.TestCase):
    def _daily(self, totals):
        start = date(2026, 7, 21)
        return [{"day": start + timedelta(days=i), "total": t,
                 "completed": max(0, t - 1)} for i, t in enumerate(totals)]

    def test_renders_sparkline_and_summary(self):
        out = stats.trend_text(self._daily([3, 5, 0, 8, 12, 7, 2]), [0] * 24, True)
        self.assertIn("روند فعالیت", out)
        self.assertIn("شلوغ‌ترین روز", out)

    def test_all_zero_says_no_activity(self):
        out = stats.trend_text(self._daily([0] * 7), [0] * 24, True)
        self.assertIn("پستی ثبت نشده", out)

    def test_weekly_bars_encode_volume_not_ratio(self):
        # A 15-post day must render a longer bar than a 3-post day. Encoding
        # the completion ratio here made busy and quiet days look identical.
        daily = self._daily([3, 3, 3, 3, 3, 3, 15])
        out = stats.trend_text(daily, [0] * 24, True)
        rows = [l for l in out.splitlines() if l.startswith("<code>")]
        busiest = max(rows, key=lambda r: r.count("█"))
        self.assertIn("۱۵", busiest)

    def test_counts_quiet_days(self):
        out = stats.trend_text(self._daily([0, 0, 5, 0, 1, 2, 3]), [0] * 24, True)
        self.assertIn("روزهای بدون پست", out)

    def test_hourly_axis_is_explicit(self):
        hourly = [0] * 24
        hourly[10] = 5
        out = stats.trend_text(self._daily([1] * 7), hourly, True)
        self.assertIn("پرکارترین ساعت", out)
        self.assertNotIn("←", out)  # RTL-ambiguous arrow axis was removed

    def test_fits_telegram_limit(self):
        out = stats.trend_text(self._daily(list(range(14))), list(range(24)), True)
        self.assertLessEqual(len(out), stats.TELEGRAM_LIMIT)


class ChannelsTests(unittest.TestCase):
    def _row(self, **over):
        base = {"channel_id": 1, "platform": "telegram", "name": "کانال",
                "is_active": 1, "last_health_status": "healthy",
                "attempted": 100, "delivered": 90, "failing": 10,
                "total_attempts": 120, "last_error": "boom",
                "last_activity": datetime(2026, 8, 3)}
        base.update(over)
        return base

    def test_shows_error_text(self):
        out = stats.channels_text([self._row(last_error="chat not found")], [], [])
        self.assertIn("chat not found", out)

    def test_healthy_channel_marked_ok(self):
        out = stats.channels_text(
            [self._row(delivered=100, failing=0, last_error=None)], [], [])
        self.assertIn("✅", out)

    def test_total_failure_marked_red(self):
        out = stats.channels_text([self._row(delivered=0, failing=100)], [], [])
        self.assertIn("🔴", out)

    def test_inactive_channel_marked(self):
        out = stats.channels_text([self._row(is_active=0)], [], [])
        self.assertIn("⏸", out)

    def test_retry_ratio_surfaces_flaky_channel(self):
        # Delivers eventually, but only after many retries.
        out = stats.channels_text(
            [self._row(delivered=100, failing=0, attempted=100,
                       total_attempts=300, last_error=None)], [], [])
        self.assertIn("میانگین تلاش", out)

    def test_no_retry_line_when_clean(self):
        out = stats.channels_text(
            [self._row(delivered=100, failing=0, attempted=100,
                       total_attempts=100, last_error=None)], [], [])
        self.assertNotIn("میانگین تلاش", out)

    def test_many_channels_stay_within_limit(self):
        rows = [self._row(channel_id=i, name=f"کانال {i}",
                          last_error="x" * 100) for i in range(40)]
        out = stats.clip(stats.channels_text(rows, [], []))
        self.assertLessEqual(len(out), stats.TELEGRAM_LIMIT)

    def test_hidden_count_is_reported(self):
        rows = [self._row(channel_id=i, name=f"کانال {i}") for i in range(30)]
        self.assertIn("کانال دیگر", stats.channels_text(rows, [], []))

    def test_empty_state(self):
        self.assertIn("ثبت نشده", stats.channels_text([], [], []))


class AuthorsTests(unittest.TestCase):
    def _row(self, **over):
        base = {"user_id": 1, "name": "علی", "role": "writer", "total": 10,
                "completed": 9, "problems": 1, "last_post": datetime(2026, 8, 3)}
        base.update(over)
        return base

    def test_ranks_and_labels_roles(self):
        out = stats.authors_text([self._row(role="sudo", name="مدیر")])
        self.assertIn("👑", out)
        self.assertIn("مدیر", out)

    def test_handles_missing_name_and_last_post(self):
        out = stats.authors_text([self._row(name="#7", role=None, last_post=None)])
        self.assertIn("#7", out)

    def test_empty_state(self):
        self.assertIn("ثبت نشده", stats.authors_text([]))

    def test_many_authors_stay_within_limit(self):
        rows = [self._row(user_id=i, name="ن" * 40) for i in range(40)]
        self.assertLessEqual(len(stats.clip(stats.authors_text(rows))),
                             stats.TELEGRAM_LIMIT)


class ScheduleStatsTests(unittest.TestCase):
    def test_reports_success_rate_and_problems(self):
        out = stats.schedule_text({
            "total": 40, "completed": 36, "failed": 1, "cancelled": 2,
            "expired": 1, "pending": 3, "avg_delay_seconds": 42.0, "upcoming": [],
        }, True)
        self.assertIn("۹۰٪", out)
        self.assertIn("منقضی", out)

    def test_explains_small_delay_as_normal(self):
        out = stats.schedule_text({
            "total": 5, "completed": 5, "pending": 0,
            "avg_delay_seconds": 30.0, "upcoming": [],
        }, True)
        self.assertIn("طبیعی", out)

    def test_large_delay_is_not_excused(self):
        out = stats.schedule_text({
            "total": 5, "completed": 5, "pending": 0,
            "avg_delay_seconds": 3600.0, "upcoming": [],
        }, True)
        self.assertNotIn("طبیعی", out)

    def test_empty_state(self):
        out = stats.schedule_text({"total": 0, "pending": 0, "upcoming": []}, True)
        self.assertIn("ثبت نشده", out)

    def test_writer_view_marked(self):
        out = stats.schedule_text({"total": 0, "pending": 0, "upcoming": []}, False)
        self.assertIn("فقط زمان‌بندی‌های شما", out)


class HumaniseTests(unittest.TestCase):
    def test_units(self):
        self.assertIn("ثانیه", stats._humanise_seconds(30))
        self.assertIn("دقیقه", stats._humanise_seconds(300))
        self.assertIn("ساعت", stats._humanise_seconds(7200))

    def test_none(self):
        self.assertEqual(stats._humanise_seconds(None), "—")


class AccessKeyboardTests(unittest.TestCase):
    def test_writer_menu_hides_admin_sections(self):
        payloads = [b.callback_data
                    for row in stats.stats_menu_keyboard(False).inline_keyboard
                    for b in row]
        self.assertNotIn("stats_channels", payloads)
        self.assertNotIn("stats_authors", payloads)
        self.assertIn("stats_summary", payloads)

    def test_admin_menu_shows_everything(self):
        payloads = [b.callback_data
                    for row in stats.stats_menu_keyboard(True).inline_keyboard
                    for b in row]
        for expected in ("stats_summary", "stats_trend", "stats_channels",
                         "stats_authors", "stats_schedule"):
            self.assertIn(expected, payloads)


class ScopeQueryTests(unittest.TestCase):
    """The SQL scope helper must parameterise, never interpolate, the user id.

    Loaded from source rather than imported: other test modules install a stub
    ``database`` module, and whichever imports first wins.
    """

    @staticmethod
    def _scope_clause():
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "_real_database_for_scope", os.path.join(root, "database.py"))
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # pragma: no cover - needs aiomysql/dotenv
            raise unittest.SkipTest(f"database.py not importable: {exc}") from exc
        return module._scope_clause

    def test_admin_scope_is_empty(self):
        sql, params = self._scope_clause()(None)
        self.assertEqual(sql, "")
        self.assertEqual(params, [])

    def test_writer_scope_is_parameterised(self):
        sql, params = self._scope_clause()(4242)
        self.assertIn("%s", sql)
        self.assertNotIn("4242", sql)
        self.assertEqual(params, [4242])

    def test_alias_is_honoured(self):
        sql, _ = self._scope_clause()(1, alias="s")
        self.assertIn("s.user_id", sql)


class RoutingTests(unittest.TestCase):
    """Every stats callback must have a handler registered."""

    def test_all_stats_callbacks_are_routed(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        source = open(os.path.join(root, "bot.py"), encoding="utf-8").read()
        patterns = [p.replace("\\\\", "\\") for p in
                    re.findall(r'CallbackQueryHandler\([^,]+,\s*pattern=r?"([^"]+)"', source)]
        for probe in ("tools_stats", "stats_summary", "stats_trend",
                      "stats_channels", "stats_authors", "stats_schedule"):
            with self.subTest(callback=probe):
                self.assertTrue(any(re.match(p, probe) for p in patterns),
                                f"{probe} has no handler")


if __name__ == "__main__":
    unittest.main()
