"""Tests for outage handling: MySQL restarts and dropped Telegram connections.

Reproduces the 2026-08-14 incident, where a host-side MySQL restart produced
three tracebacks per 60-second tick — one of them reported as "Unhandled
exception while processing update" even though no update was involved.
"""

import asyncio
import logging
import os
import sys
import types
import unittest
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
# Also importable as ``tests.test_resilience``, where unittest does not put the
# tests directory itself on the path.
sys.path.insert(0, _HERE)

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("BALE_TOKEN", "test-token")
os.environ.setdefault("SUDO_USER_ID", "1")

from resilience import (  # noqa: E402
    TransientErrorReporter, describe, is_transient_infra_error,
)

# Reuse the in-memory database stub and fixtures from the scheduling suite so
# these tests exercise the real job functions.
from test_scheduling import (  # noqa: E402
    FAKE, SchedulingTestCase, make_context, post, run,
)


def mysql_down_error():
    """The exact exception chain aiomysql raises when MySQL is not listening."""
    class OperationalError(Exception):
        pass

    os_err = OSError(
        "Multiple exceptions: [Errno 111] Connect call failed ('::1', 3306, 0, 0), "
        "[Errno 111] Connect call failed ('127.0.0.1', 3306)"
    )
    try:
        try:
            raise os_err
        except OSError as cause:
            raise OperationalError(
                (2003, "Can't connect to MySQL server on 'localhost'")
            ) from cause
    except OperationalError as exc:
        return exc


def telegram_read_error():
    """httpx.ReadError wrapping httpcore.ReadError, as the Updater raises it."""
    class HttpcoreReadError(Exception):
        __name__ = "ReadError"

    class ReadError(Exception):
        pass

    try:
        try:
            raise HttpcoreReadError()
        except HttpcoreReadError as cause:
            raise ReadError("") from cause
    except ReadError as exc:
        return exc


class ClassificationTests(unittest.TestCase):
    """Outages must be recognised; bugs must not be mistaken for outages."""

    def test_mysql_connection_refused_is_transient(self):
        self.assertTrue(is_transient_infra_error(mysql_down_error()))

    def test_bare_econnrefused_is_transient(self):
        self.assertTrue(is_transient_infra_error(ConnectionRefusedError(111, "refused")))

    def test_telegram_read_error_is_transient(self):
        self.assertTrue(is_transient_infra_error(telegram_read_error()))

    def test_pool_exhaustion_is_transient(self):
        self.assertTrue(is_transient_infra_error(
            RuntimeError("Could not connect to MySQL after 3 attempts")))

    def test_programming_mistakes_are_not_transient(self):
        for exc in (KeyError("post_id"), TypeError("bad arg"),
                    ValueError("Invalid schedule status: nope"),
                    AttributeError("no attribute")):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(is_transient_infra_error(exc),
                                 "a real bug must keep its traceback")

    def test_none_is_not_transient(self):
        self.assertFalse(is_transient_infra_error(None))

    def test_describe_is_a_single_short_line(self):
        text = describe(mysql_down_error())
        self.assertNotIn("\n", text)
        self.assertLessEqual(len(text), 300)
        self.assertIn("111", text)


class ReporterTests(unittest.TestCase):
    """One outage must produce one log line, not one per tick."""

    def setUp(self):
        self.log = logging.getLogger("test.resilience")
        self.log.propagate = False
        self.records = []

        class Capture(logging.Handler):
            def emit(_self, record):
                self.records.append(record)

        self.handler = Capture()
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.INFO)

    def tearDown(self):
        self.log.removeHandler(self.handler)

    def warnings(self):
        return [r for r in self.records if r.levelno == logging.WARNING]

    def test_repeated_failures_log_once(self):
        r = TransientErrorReporter("database", self.log)
        for _ in range(60):  # an hour of 60-second ticks
            self.assertTrue(r.report(mysql_down_error(), "Due schedule lookup"))
        self.assertEqual(len(self.warnings()), 1,
                         "an outage must not log once per tick")
        self.assertEqual(r.failures, 60)

    def test_long_outage_logs_a_reminder(self):
        r = TransientErrorReporter("database", self.log, repeat_after=900)
        clock = [0.0]
        r._monotonic = lambda: clock[0]
        r.report(mysql_down_error(), "ctx")
        clock[0] = 800
        r.report(mysql_down_error(), "ctx")
        self.assertEqual(len(self.warnings()), 1, "too early for a reminder")
        clock[0] = 1000
        r.report(mysql_down_error(), "ctx")
        self.assertEqual(len(self.warnings()), 2, "a long outage should remind once")

    def test_recovery_is_reported_with_a_count(self):
        r = TransientErrorReporter("database", self.log)
        for _ in range(5):
            r.report(mysql_down_error(), "ctx")
        r.clear("Scheduled posts job")
        info = [x for x in self.records if x.levelno == logging.INFO]
        self.assertTrue(info, "recovery must be logged")
        self.assertIn("5", info[-1].getMessage())
        self.assertEqual(r.failures, 0)

    def test_clear_is_silent_when_nothing_failed(self):
        r = TransientErrorReporter("database", self.log)
        r.clear("ctx")
        self.assertEqual(self.records, [])

    def test_real_bugs_are_not_swallowed(self):
        r = TransientErrorReporter("database", self.log)
        self.assertFalse(r.report(KeyError("post_id"), "ctx"),
                         "the caller must still log a bug itself")
        self.assertEqual(self.records, [])

    def test_second_outage_after_recovery_logs_again(self):
        r = TransientErrorReporter("database", self.log)
        r.report(mysql_down_error(), "ctx")
        r.clear("ctx")
        r.report(mysql_down_error(), "ctx")
        self.assertEqual(len(self.warnings()), 2)


class JobOutageTests(SchedulingTestCase):
    """The periodic jobs must survive a database outage quietly."""

    def _break_db(self, *names):
        def boom(*a, **k):
            raise mysql_down_error()

        async def afail(*a, **k):
            boom()

        for name in names:
            setattr(post, name, afail)

    def test_scheduled_job_does_not_raise_when_mysql_is_down(self):
        post._schedule_db_outage.failures = 0
        self._break_db("reclaim_stale_schedules", "expire_stale_schedules",
                       "get_due_schedules")
        # Must not propagate: escalating to the global handler is what produced
        # the misleading "Unhandled exception while processing update".
        run(post.process_scheduled_posts(make_context()))
        self.assertGreater(post._schedule_db_outage.failures, 0)

    def test_outage_logs_once_per_tick_not_three_times(self):
        post._schedule_db_outage.failures = 0
        self._break_db("reclaim_stale_schedules", "expire_stale_schedules",
                       "get_due_schedules")
        with self.assertLogs("handlers.post", level="WARNING") as cm:
            for _ in range(3):
                run(post.process_scheduled_posts(make_context()))
        self.assertEqual(len(cm.output), 1,
                         "three ticks of one outage must produce one warning")
        self.assertEqual(post._schedule_db_outage.failures, 3,
                         "each tick counts as one failure, not three")

    def test_retry_job_does_not_raise_when_mysql_is_down(self):
        post._retry_db_outage.failures = 0
        self._break_db("reclaim_stale_retries", "get_due_retries")
        run(post.process_delivery_retries(make_context()))
        self.assertGreater(post._retry_db_outage.failures, 0)

    def test_schedules_still_publish_after_the_database_comes_back(self):
        """The rows survive the outage untouched and go out on a later tick."""
        post._schedule_db_outage.failures = 0
        pid = run(FAKE.save_post(7, "text", text="hi",
                                 target_channels_json="[1]",
                                 delivery_status="scheduled"))
        sid = run(FAKE.create_schedule(7, pid, datetime.utcnow() - timedelta(minutes=1)))

        calls = []

        async def fake_publish(p, bot, only_channel_ids=None, attempt_no=1):
            calls.append(p["id"])
            return 1, 0

        original = post.publish_existing_post
        post.publish_existing_post = fake_publish
        healthy = {name: getattr(post, name) for name in
                   ("reclaim_stale_schedules", "expire_stale_schedules",
                    "get_due_schedules")}
        try:
            self._break_db(*healthy)
            run(post.process_scheduled_posts(make_context()))
            self.assertEqual(calls, [], "nothing may publish while the DB is down")
            self.assertEqual(FAKE.schedules[sid]["status"], "scheduled",
                             "the row must be left untouched for a later tick")
            self.assertEqual(FAKE.schedules[sid]["attempts"], 0,
                             "a DB outage must not burn a retry attempt")

            for name, fn in healthy.items():  # MySQL comes back
                setattr(post, name, fn)
            run(post.process_scheduled_posts(make_context()))
        finally:
            post.publish_existing_post = original
            for name, fn in healthy.items():
                setattr(post, name, fn)

        self.assertEqual(calls, [pid], "the post must publish once the DB returns")
        self.assertEqual(FAKE.schedules[sid]["status"], "completed")
        self.assertEqual(post._schedule_db_outage.failures, 0,
                         "recovery must reset the outage counter")

    def test_a_real_bug_in_a_sweep_still_logs_a_traceback(self):
        post._schedule_db_outage.failures = 0

        async def bug(*a, **k):
            raise KeyError("attempts")

        post.reclaim_stale_schedules = bug
        with self.assertLogs("handlers.post", level="ERROR") as cm:
            run(post.process_scheduled_posts(make_context()))
        self.assertTrue(any("Stale schedule recovery failed" in line
                            for line in cm.output))


class ErrorHandlerTests(unittest.TestCase):
    """bot.on_error must describe what actually failed."""

    def setUp(self):
        try:
            import bot  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise unittest.SkipTest(f"bot module unavailable: {exc}") from exc
        self.bot_mod = sys.modules["bot"]

    def _ctx(self, error, job=None):
        return types.SimpleNamespace(error=error, job=job)

    def test_job_failure_is_not_called_an_update(self):
        job = types.SimpleNamespace(name="scheduled_posts")
        with self.assertLogs("bot", level="WARNING") as cm:
            asyncio.run(self.bot_mod.on_error(None, self._ctx(mysql_down_error(), job)))
        text = "\n".join(cm.output)
        self.assertNotIn("processing update", text,
                         "a job failure must not be reported as an update failure")
        self.assertIn("scheduled_posts", text)

    def test_outage_is_a_warning_not_an_unhandled_error(self):
        with self.assertLogs("bot", level="WARNING") as cm:
            asyncio.run(self.bot_mod.on_error(None, self._ctx(mysql_down_error())))
        record = cm.records[-1]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertNotIn("Unhandled exception", record.getMessage())

    def test_real_bug_is_still_an_error(self):
        with self.assertLogs("bot", level="ERROR") as cm:
            asyncio.run(self.bot_mod.on_error(None, self._ctx(KeyError("boom"))))
        self.assertIn("Unhandled exception", cm.records[-1].getMessage())

    def test_user_facing_update_still_gets_a_reply(self):
        replies = []

        class Msg:
            async def reply_text(_self, text, **kw):
                replies.append(text)

        update = types.SimpleNamespace(callback_query=None, effective_message=Msg())
        with self.assertLogs("bot", level="ERROR"):
            asyncio.run(self.bot_mod.on_error(update, self._ctx(KeyError("boom"))))
        self.assertEqual(len(replies), 1,
                         "the user must still be told something went wrong")

    def test_callback_query_is_still_answered(self):
        answered = []

        class Query:
            async def answer(_self, text=None, show_alert=False):
                answered.append(text)

        update = types.SimpleNamespace(callback_query=Query())
        with self.assertLogs("bot", level="WARNING"):
            asyncio.run(self.bot_mod.on_error(update, self._ctx(telegram_read_error())))
        self.assertEqual(len(answered), 1,
                         "the button must be released so the client stops spinning")


if __name__ == "__main__":
    unittest.main()
