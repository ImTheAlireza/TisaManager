"""Tests for startup sequencing and job misfire windows.

Locks down the 2026-08-14 21:16 incident:

    apscheduler.executors.default - WARNING
    Run time of job "initialize_database (...)" was missed by 0:00:01.433134

``initialize_database`` was a ``run_once(when=0)`` job, so it inherited
APScheduler's default ``misfire_grace_time`` of 1 second. APScheduler does not
run a late job — it logs that warning and skips it. The bot therefore came up
polling with no database, no restored workflows and no periodic jobs at all.
"""

import ast
import asyncio
import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("BALE_TOKEN", "test-token")
os.environ.setdefault("SUDO_USER_ID", "1")


class MisfireSemanticsTests(unittest.TestCase):
    """Prove the framework behaviour the fix is built on."""

    def test_apscheduler_skips_a_job_that_misses_its_grace_window(self):
        """A missed job is dropped, not run late. This is why it mattered."""
        try:
            from apscheduler.executors.base import run_coroutine_job
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("apscheduler not installed")
        import inspect
        src = inspect.getsource(run_coroutine_job)
        self.assertIn("was missed by", src)
        # The warning is immediately followed by `continue` — the job never runs.
        after = src.split("was missed by")[1]
        self.assertIn("continue", after.split("Running job")[0],
                      "a misfired job must be understood to be skipped entirely")

    def test_default_misfire_grace_time_is_one_second(self):
        try:
            from apscheduler.schedulers.base import BaseScheduler
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("apscheduler not installed")
        import inspect
        src = inspect.getsource(BaseScheduler._configure)
        self.assertIn('misfire_grace_time", 1', src.replace("'", '"'),
                      "the 1s default is the hazard these tests guard against")


class StartupWiringTests(unittest.TestCase):
    """Startup must not run through the scheduler at all."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(_ROOT, "bot.py"), encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.tree = ast.parse(cls.source)

    def test_startup_is_not_a_run_once_job(self):
        self.assertNotIn("run_once(initialize_database", self.source,
                         "startup must not depend on a misfire-prone job")
        self.assertNotIn("run_once(notify_online", self.source,
                         "the online notice must not be a separate racy job")

    def test_no_run_once_jobs_remain_in_startup_path(self):
        self.assertNotIn("job_queue.run_once", self.source,
                         "run_once inherits the 1s grace default")

    def test_startup_uses_post_init(self):
        self.assertIn("post_init", self.source,
                      "startup must be awaited by the framework, not scheduled")

    def test_startup_is_a_coroutine(self):
        import bot
        self.assertTrue(asyncio.iscoroutinefunction(bot.startup))

    def test_init_db_is_awaited_before_jobs_are_registered(self):
        """Periodic jobs must never run against an unmigrated database."""
        import bot
        src = __import__("inspect").getsource(bot.startup)
        self.assertLess(src.index("init_db"), src.index("register_periodic_jobs"))


class MisfireGraceTimeTests(unittest.TestCase):
    """Every recurring job needs an explicit, adequate grace window."""

    def setUp(self):
        try:
            import telegram  # noqa: F401
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("python-telegram-bot required")

    def _registered_jobs(self):
        """Run register_periodic_jobs against a real (paused) JobQueue."""
        import bot
        from telegram.ext import JobQueue

        async def build():
            jq = JobQueue()

            class FakeApp:
                bot = None

                def create_task(self, coro, **kw):
                    return asyncio.ensure_future(coro)

            app = FakeApp()
            jq._application = lambda: app
            bot.register_periodic_jobs(jq)
            jq.scheduler.start(paused=True)
            jobs = {j.name: j.job.misfire_grace_time for j in jq.jobs()}
            jq.scheduler.shutdown(wait=False)
            return jobs

        return asyncio.run(build())

    def test_every_job_sets_an_explicit_grace_time(self):
        for name, grace in self._registered_jobs().items():
            with self.subTest(job=name):
                self.assertIsNotNone(grace, f"{name} has no misfire_grace_time")
                self.assertGreater(
                    grace, 1,
                    f"{name} is on (or below) the 1s default and can be skipped",
                )

    def test_frequent_jobs_tolerate_a_realistic_stall(self):
        jobs = self._registered_jobs()
        # A minute-cadence job must survive a multi-second host stall.
        self.assertGreaterEqual(jobs["scheduled_posts"], 60)
        self.assertGreaterEqual(jobs["delivery_retries"], 60)

    def test_daily_jobs_have_generous_windows(self):
        """A skipped daily job waits 24h for another chance."""
        jobs = self._registered_jobs()
        self.assertGreaterEqual(jobs["daily_report"], 600)
        self.assertGreaterEqual(jobs["nightly_backup"], 600)

    def test_all_expected_jobs_are_registered(self):
        self.assertEqual(
            set(self._registered_jobs()),
            {"scheduled_posts", "delivery_retries", "channel_health",
             "daily_report", "nightly_backup", "history_prune"},
        )


class StartupBehaviourTests(unittest.TestCase):
    """startup() must migrate, restore, register and announce — in order."""

    def setUp(self):
        try:
            import bot  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise unittest.SkipTest(f"bot unavailable: {exc}") from exc
        self.bot = sys.modules["bot"]

    def _patch(self, **kw):
        originals = {k: getattr(self.bot, k) for k in kw}
        for k, v in kw.items():
            setattr(self.bot, k, v)
        self.addCleanup(lambda: [setattr(self.bot, k, v)
                                 for k, v in originals.items()])

    def _run_startup(self, **overrides):
        calls = []

        async def init_db():
            calls.append("init_db")

        async def load_calendar_preference():
            calls.append("calendar")

        async def restore_workflow_states(ctx):
            calls.append("restore")
            return 0

        def register_periodic_jobs(jq):
            calls.append("jobs")

        async def notify_online(ctx):
            calls.append("notify")

        defaults = dict(
            init_db=init_db,
            load_calendar_preference=load_calendar_preference,
            restore_workflow_states=restore_workflow_states,
            register_periodic_jobs=register_periodic_jobs,
            notify_online=notify_online,
        )
        defaults.update(overrides)
        self._patch(**defaults)

        app = types.SimpleNamespace(bot=object(), job_queue=object())
        asyncio.run(self.bot.startup(app))
        return calls

    def test_startup_runs_the_full_sequence_in_order(self):
        calls = self._run_startup()
        self.assertEqual(calls, ["init_db", "calendar", "restore", "jobs", "notify"])

    def test_jobs_are_registered_only_after_the_database_is_ready(self):
        calls = self._run_startup()
        self.assertLess(calls.index("init_db"), calls.index("jobs"))

    def test_online_notice_comes_after_jobs_are_live(self):
        """Announcing 'ready' before the jobs exist is a lie to the owner."""
        calls = self._run_startup()
        self.assertLess(calls.index("jobs"), calls.index("notify"))

    def test_a_failing_notification_does_not_abort_startup(self):
        async def boom(ctx):
            raise RuntimeError("telegram unreachable")

        calls = self._run_startup(notify_online=boom)
        self.assertIn("jobs", calls, "jobs must survive a failed notification")

    def test_a_failing_restore_does_not_prevent_job_registration(self):
        async def boom(ctx):
            raise RuntimeError("bad payload")

        calls = self._run_startup(restore_workflow_states=boom)
        self.assertIn("jobs", calls,
                      "a restore failure must not leave the bot with no jobs")

    def test_database_failure_propagates(self):
        """A dead database at boot must be loud, not a silently crippled bot."""
        async def boom():
            raise RuntimeError("Could not connect to MySQL after 3 attempts")

        with self.assertRaises(RuntimeError):
            self._run_startup(init_db=boom)


if __name__ == "__main__":
    unittest.main()
