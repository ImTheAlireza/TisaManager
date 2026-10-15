"""Tests for the health dashboard: bot availability, channel status and rendering."""

import asyncio
import os
import sys
import types
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("BALE_TOKEN", "test-token")
os.environ.setdefault("SUDO_USER_ID", "1")


def _install_db_stub():
    """Stub ``database`` so importing handlers.admin needs no live MySQL.

    Mirrors the stub in the other handler test modules; if one of them already
    installed a fake, reuse it so the whole suite shares a single stand-in.
    """
    if "database" in sys.modules:
        return
    db = types.ModuleType("database")
    _LIST_RETURNING = {
        "get_active_channels", "get_due_schedules", "get_user_posts", "get_all_posts",
        "get_channel_health", "get_channel_groups", "list_users", "get_all_users",
    }
    _TRUE_RETURNING = {
        "is_sudo", "is_owner", "is_writer_or_above", "has_permission", "add_channel",
        "remove_channel", "create_channel_group", "save_post",
    }

    def _make(name):
        async def _stub(*args, **kwargs):
            if name in _LIST_RETURNING:
                return []
            if name in _TRUE_RETURNING:
                return True
            return None
        _stub.__name__ = name
        return _stub

    def __getattr__(name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _make(name)

    db.__getattr__ = __getattr__
    sys.modules["database"] = db


_install_db_stub()

try:
    import handlers.admin as admin
    import bale_client
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"python-telegram-bot required: {exc}") from exc


def run(coro):
    return asyncio.run(coro)


class ClassifyBaleChannelTests(unittest.TestCase):
    """Per-bot failures map to healthy / degraded / unhealthy."""

    def test_all_bots_reach_the_channel(self):
        self.assertEqual(admin.classify_bale_channel([], 2), ("healthy", None))

    def test_one_bot_failing_is_degraded_not_unhealthy(self):
        # Attempts alternate bots, so with one bot down the post still goes out
        # on every other attempt — worth a warning, not a red alert.
        status, error = admin.classify_bale_channel(["bale-2: Forbidden"], 2)
        self.assertEqual(status, "degraded")
        self.assertIn("bale-2", error)

    def test_all_bots_failing_is_unhealthy(self):
        status, error = admin.classify_bale_channel(
            ["bale-1: Forbidden", "bale-2: Forbidden"], 2)
        self.assertEqual(status, "unhealthy")
        self.assertIn("bale-1", error)
        self.assertIn("bale-2", error)

    def test_single_bot_failure_is_unhealthy(self):
        # Without a backup there is nothing to fall back to.
        status, _ = admin.classify_bale_channel(["bale-1: Forbidden"], 1)
        self.assertEqual(status, "unhealthy")


class LoadBotHealthTests(unittest.TestCase):
    def test_valid_json_is_loaded(self):
        data = admin.load_bot_health('{"telegram": {"ok": true}}')
        self.assertEqual(data["telegram"]["ok"], True)

    def test_invalid_json_returns_none(self):
        self.assertIsNone(admin.load_bot_health("not json"))
        self.assertIsNone(admin.load_bot_health(None))
        self.assertIsNone(admin.load_bot_health("[1, 2]"))


class HealthTextTests(unittest.TestCase):
    """The dashboard rendering."""

    def _rows(self):
        return [
            {"id": 1, "name": "TG Channel", "platform": "telegram", "is_active": True,
             "last_health_status": "healthy", "last_health_error": None,
             "last_health_check": datetime(2026, 8, 5, 8, 0)},
            {"id": 2, "name": "Bale Channel", "platform": "bale", "is_active": True,
             "last_health_status": "degraded", "last_health_error": "bale-2: Forbidden",
             "last_health_check": datetime(2026, 8, 5, 8, 0)},
            {"id": 3, "name": "New Channel", "platform": "telegram", "is_active": True,
             "last_health_status": None, "last_health_error": None,
             "last_health_check": None},
            {"id": 4, "name": "Old Channel", "platform": "telegram", "is_active": False,
             "last_health_status": "healthy", "last_health_error": None,
             "last_health_check": None},
        ]

    def test_bot_section_reports_every_bot(self):
        bot_health = {
            "telegram": {"ok": True, "name": "@main_bot"},
            "bale-1": {"ok": True, "name": "@bale_one"},
            "bale-2": {"ok": False, "error": "bad token"},
            "checked_at": "2026-08-05T08:00:00",
        }
        text = admin._health_text(self._rows(), bot_health, backup_configured=True)
        self.assertIn("@main_bot", text)
        self.assertIn("@bale_one", text)
        self.assertIn("❌ ربات بله ۲", text)
        self.assertIn("bad token", text)
        self.assertIn("آخرین بررسی", text)

    def test_missing_backup_is_reported_as_not_configured(self):
        bot_health = {
            "telegram": {"ok": True, "name": "@main_bot"},
            "bale-1": {"ok": True, "name": "@bale_one"},
            "checked_at": "2026-08-05T08:00:00",
        }
        text = admin._health_text(self._rows(), bot_health, backup_configured=False)
        self.assertIn("پیکربندی نشده", text)
        self.assertIn("BALE_TOKEN_2", text)

    def test_channel_rows_show_status_error_and_time(self):
        text = admin._health_text(self._rows(), None, backup_configured=False)
        self.assertIn("TG Channel — ✅ سالم", text)
        self.assertIn("Bale Channel — ⚠️ نیمه‌سالم", text)
        self.assertIn("bale-2: Forbidden", text)
        self.assertIn("New Channel — ⚪ بررسی نشده", text)
        self.assertIn("Old Channel — ⏸️ غیرفعال", text)
        self.assertIn("(3 فعال)", text)

    def test_never_checked_says_so(self):
        text = admin._health_text([], None, backup_configured=False)
        self.assertIn("هنوز بررسی‌ای انجام نشده", text)
        self.assertIn("کانالی تنظیم نشده", text)

    def test_active_bale_route_is_shown(self):
        """Which relay the Bale calls go through, and where it points.

        A day-long Bale outage is usually an outage of the relay host, and the
        dashboard is where the operator looks first — not .env.
        """
        transport = {"route": "bridge", "fa": "پل ایران (bridge)",
                     "active_fa": "پل ایران (bridge)",
                     "detail": "https://site.example/fp9x2/bale_bridge.php",
                     "bridge_host": "site.example",
                     "fallback_fa": "پروکسی", "paused": False}

        text = admin._health_text(self._rows(), None, backup_configured=True, transport=transport)

        self.assertIn("مسیر ارسال بله: پل ایران (bridge)", text)
        self.assertIn("مسیر جایگزین: پروکسی", text)
        self.assertIn("هاست پل: <code>site.example</code>", text)

    def test_a_paused_relay_is_shown_as_skipped(self):
        """The relay that is blocking us must not look like the active route."""
        transport = {"route": "bridge", "fa": "پل ایران (bridge)",
                     "active_fa": "مستقیم",
                     "detail": "https://site.example/fp9x2/bale_bridge.php",
                     "bridge_host": "site.example",
                     "fallback_fa": "پل ایران (bridge)", "paused": True}

        text = admin._health_text(self._rows(), None, backup_configured=True, transport=transport)

        self.assertIn("مسیر ارسال بله: مستقیم", text)
        self.assertIn("پل موقتاً کنار گذاشته شده", text)
        self.assertIn("هاست پل: <code>site.example</code>", text)

    def test_no_transport_line_without_bale(self):
        text = admin._health_text(self._rows(), None, backup_configured=False)
        self.assertNotIn("مسیر ارسال بله", text)

    def test_a_down_bale_bot_points_at_the_doctor(self):
        bot_health = {"bale-1": {"ok": False, "error": "403 — the bridge host refused it"},
                      "checked_at": "2026-08-05T08:00:00"}

        text = admin._health_text(self._rows(), bot_health, backup_configured=False)

        self.assertIn("diagnose_bale.py", text)

    def test_a_healthy_bale_bot_does_not_point_at_the_doctor(self):
        bot_health = {"bale-1": {"ok": True, "name": "@bale_one"},
                      "checked_at": "2026-08-05T08:00:00"}

        text = admin._health_text(self._rows(), bot_health, backup_configured=False)

        self.assertNotIn("diagnose_bale.py", text)


class BotAvailabilityTests(unittest.TestCase):
    """run_bot_health_checks reports the Telegram bot and every Bale bot."""

    def test_each_bot_is_checked_and_reported(self):
        class FakeBale:
            def __init__(self, name, ok, description=""):
                self.name = name
                self._ok = ok
                self._description = description

            async def get_me(self):
                if self._ok:
                    return {"ok": True, "result": {"username": f"{self.name}_user"}}
                return {"ok": False, "description": self._description}

        class FakeTgBot:
            async def get_me(self):
                return types.SimpleNamespace(username="main_bot", full_name="Main")

        clients = [FakeBale("bale-1", True), FakeBale("bale-2", False, "bad token")]
        original = bale_client.all_clients
        bale_client.all_clients = lambda: clients
        try:
            context = types.SimpleNamespace(bot=FakeTgBot())
            result = run(admin.run_bot_health_checks(context))
        finally:
            bale_client.all_clients = original

        self.assertTrue(result["telegram"]["ok"])
        self.assertEqual(result["telegram"]["name"], "@main_bot")
        self.assertTrue(result["bale-1"]["ok"])
        self.assertEqual(result["bale-1"]["name"], "@bale-1_user")
        self.assertFalse(result["bale-2"]["ok"])
        self.assertIn("bad token", result["bale-2"]["error"])
        self.assertIn("checked_at", result, "the report must carry a timestamp")

    def test_unreachable_bot_is_reported_not_raised(self):
        class BrokenBale:
            name = "bale-1"

            async def get_me(self):
                raise RuntimeError("connection refused")

        original = bale_client.all_clients
        bale_client.all_clients = lambda: [BrokenBale()]
        try:
            context = types.SimpleNamespace(
                bot=types.SimpleNamespace(
                    get_me=lambda: _ok_me(),
                ),
            )
            result = run(admin.run_bot_health_checks(context))
        finally:
            bale_client.all_clients = original

        self.assertFalse(result["bale-1"]["ok"])
        self.assertIn("connection refused", result["bale-1"]["error"])


async def _ok_me():
    return types.SimpleNamespace(username="main_bot", full_name="Main")


if __name__ == "__main__":
    unittest.main()
