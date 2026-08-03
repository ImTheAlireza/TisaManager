"""Regression tests for the scheduling, retry and restart-safety fixes.

Each test names the failure mode it locks down. The database module is stubbed
with an in-memory fake so the whole flow can run without MySQL.
"""

import asyncio
import os
import sys
import time
import types
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("BALE_TOKEN", "test-token")
os.environ.setdefault("SUDO_USER_ID", "1")


# --------------------------------------------------------------------------
# In-memory stand-in for database.py
# --------------------------------------------------------------------------
class FakeDB:
    def __init__(self):
        self.reset()

    def reset(self):
        self.schedules = {}
        self.posts = {}
        self.deliveries = {}
        self.sessions = {}
        self.settings = {"approval_required": "0"}
        self.roles = {}
        self._sched_id = 0
        self._post_id = 0
        self._delivery_id = 0
        self.published = []

    # --- schedules ---
    async def create_schedule(self, user_id, post_id, run_at):
        self._sched_id += 1
        self.schedules[self._sched_id] = {
            "id": self._sched_id, "user_id": user_id, "post_id": post_id,
            "run_at": run_at, "status": "scheduled", "attempts": 0,
            "claimed_at": None, "error": None,
        }
        return self._sched_id

    async def get_due_schedules(self):
        now = datetime.utcnow()
        return [dict(s) for s in self.schedules.values()
                if s["status"] == "scheduled" and s["run_at"] <= now]

    async def claim_schedule(self, schedule_id):
        s = self.schedules.get(schedule_id)
        if not s or s["status"] != "scheduled":
            return False
        s["status"] = "processing"
        s["claimed_at"] = datetime.utcnow()
        s["attempts"] += 1
        return True

    async def update_schedule(self, schedule_id, status, error=None):
        s = self.schedules.get(schedule_id)
        if s:
            s["status"] = status
            s["error"] = error

    async def get_schedule(self, schedule_id):
        s = self.schedules.get(schedule_id)
        return dict(s) if s else None

    async def get_active_schedule_for_post(self, post_id):
        for s in self.schedules.values():
            if s["post_id"] == post_id and s["status"] in ("scheduled", "processing"):
                return dict(s)
        return None

    async def cancel_schedule(self, schedule_id):
        s = self.schedules.get(schedule_id)
        if not s or s["status"] != "scheduled":
            return False
        s["status"] = "cancelled"
        return True

    async def reschedule(self, schedule_id, run_at):
        s = self.schedules.get(schedule_id)
        if not s or s["status"] != "scheduled":
            return False
        s["run_at"] = run_at
        return True

    async def reclaim_stale_schedules(self):
        return []

    async def expire_stale_schedules(self, grace_seconds):
        cutoff = datetime.utcnow() - timedelta(seconds=grace_seconds)
        out = []
        for s in self.schedules.values():
            if s["status"] == "scheduled" and s["run_at"] < cutoff:
                s["status"] = "expired"
                out.append(dict(s))
        return out

    async def get_pending_schedules(self, user_id=None, limit=50):
        rows = [dict(s) for s in self.schedules.values()
                if s["status"] in ("scheduled", "processing")
                and (user_id is None or s["user_id"] == user_id)]
        return sorted(rows, key=lambda r: r["run_at"])

    # --- posts ---
    async def save_post(self, user_id, post_type, **kw):
        self._post_id += 1
        self.posts[self._post_id] = {
            "id": self._post_id, "user_id": user_id, "post_type": post_type,
            "delivery_status": kw.get("delivery_status", "pending"),
            "text": kw.get("text"), "file_id": kw.get("file_id"),
            "caption": kw.get("caption"), "media_json": kw.get("media_json"),
            "target_channels_json": kw.get("target_channels_json"),
            "tg_message_ids": None,
        }
        return self._post_id

    async def get_post(self, post_id):
        p = self.posts.get(post_id)
        return dict(p) if p else None

    async def update_post_status(self, post_id, status):
        if post_id in self.posts:
            self.posts[post_id]["delivery_status"] = status

    async def update_post_delivery(self, post_id, status, error=None):
        if post_id in self.posts:
            self.posts[post_id]["delivery_status"] = status

    async def update_post_message_ids(self, post_id, ids, bale=None):
        if post_id in self.posts:
            self.posts[post_id]["tg_message_ids"] = ids

    # --- deliveries ---
    async def record_delivery(self, post_id, channel_id, platform, status,
                              error=None, next_retry_at=None):
        key = (post_id, channel_id)
        row = self.deliveries.get(key)
        if row is None:
            self._delivery_id += 1
            row = {"id": self._delivery_id, "post_id": post_id, "channel_id": channel_id,
                   "platform": platform, "attempts": 0}
            self.deliveries[key] = row
        row.update({"status": status, "error": error,
                    "next_retry_at": next_retry_at, "attempts": row["attempts"] + 1})

    async def get_due_retries(self, limit=50):
        now = datetime.utcnow()
        return [dict(r) for r in self.deliveries.values()
                if r.get("status") == "failed" and r.get("next_retry_at")
                and r["next_retry_at"] <= now]

    async def claim_delivery_retry(self, delivery_id):
        for r in self.deliveries.values():
            if r["id"] == delivery_id and r.get("status") == "failed":
                r["status"] = "retrying"
                r["next_retry_at"] = None
                return True
        return False

    async def reclaim_stale_retries(self):
        return 0

    async def get_post_deliveries(self, post_id):
        return [dict(r) for r in self.deliveries.values() if r["post_id"] == post_id]

    # --- sessions ---
    async def save_workflow_session(self, user_id, kind, payload):
        import json
        self.sessions[user_id] = {"user_id": user_id, "kind": kind,
                                  "payload": json.dumps(payload)}

    async def load_workflow_sessions(self, max_age):
        return [dict(v) for v in self.sessions.values()]

    async def delete_workflow_session(self, user_id):
        self.sessions.pop(user_id, None)

    async def purge_workflow_sessions(self, max_age):
        return None

    # --- misc ---
    async def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    async def get_active_channels(self, platform=None):
        chans = [
            {"id": 1, "chat_id": -100, "name": "A", "chat_type": "channel", "platform": "telegram"},
            {"id": 2, "chat_id": -200, "name": "B", "chat_type": "channel", "platform": "telegram"},
        ]
        if platform:
            return [c for c in chans if c["platform"] == platform]
        return chans

    async def is_writer_or_above(self, user_id):
        return self.roles.get(user_id, "writer") is not None

    async def has_permission(self, user_id, permission):
        return self.roles.get(user_id) in ("sudo", "owner")

    async def get_user_role(self, user_id):
        return self.roles.get(user_id, "writer")

    async def is_sudo(self, user_id):
        return self.roles.get(user_id) == "sudo"

    async def is_owner(self, user_id):
        return self.roles.get(user_id) in ("sudo", "owner")


FAKE = FakeDB()


def _install_db_stub():
    if "database" in sys.modules and getattr(sys.modules["database"], "_is_fake", False):
        return
    db = types.ModuleType("database")
    db._is_fake = True

    def __getattr__(name):
        if name.startswith("__"):
            raise AttributeError(name)
        attr = getattr(FAKE, name, None)
        if attr is not None:
            return attr

        async def _noop(*a, **k):
            return None
        return _noop

    db.__getattr__ = __getattr__
    db.DELIVERY_STATUSES = {"draft", "scheduled", "pending_approval", "pending",
                            "completed", "partial", "failed"}
    db.SCHEDULE_STATUSES = {"scheduled", "processing", "completed", "failed",
                            "cancelled", "expired"}
    sys.modules["database"] = db


_install_db_stub()

try:
    import handlers.post as post
    import handlers.schedules as schedules
    from utils import local_to_utc_naive, utc_naive_to_local, now_local
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"python-telegram-bot required: {exc}") from exc


# The handler modules bind their database helpers at import time. Another test
# module may have installed its own stub first, so rebind the names we exercise
# onto the fake explicitly — this keeps the suite order-independent.
_REBIND = (
    "create_schedule", "get_due_schedules", "claim_schedule", "update_schedule",
    "get_schedule", "get_active_schedule_for_post", "cancel_schedule", "reschedule",
    "reclaim_stale_schedules", "expire_stale_schedules", "get_pending_schedules",
    "save_post", "get_post", "update_post_status", "update_post_delivery",
    "update_post_message_ids", "record_delivery", "get_due_retries",
    "claim_delivery_retry", "reclaim_stale_retries", "get_post_deliveries",
    "save_workflow_session", "load_workflow_sessions", "delete_workflow_session",
    "purge_workflow_sessions", "get_setting", "get_active_channels",
    "is_writer_or_above", "has_permission", "get_user_role", "is_sudo", "is_owner",
)


def _rebind_fakes():
    for module in (post, schedules):
        for name in _REBIND:
            if hasattr(module, name) and hasattr(FAKE, name):
                setattr(module, name, getattr(FAKE, name))


_rebind_fakes()


def run(coro):
    return asyncio.run(coro)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return types.SimpleNamespace(message_id=len(self.sent))


def make_context(bot=None):
    return types.SimpleNamespace(bot=bot or FakeBot(), job=None, job_queue=None)


class SchedulingTestCase(unittest.TestCase):
    """Common setup: fresh fake DB and freshly bound fake helpers."""

    def setUp(self):
        FAKE.reset()
        _rebind_fakes()
        post.user_states.clear()

    def tearDown(self):
        post.user_states.clear()


class ClaimingTests(SchedulingTestCase):
    """P0 #3 — a due row must be publishable exactly once."""

    def test_only_one_claim_succeeds(self):
        sid = run(FAKE.create_schedule(7, 1, datetime.utcnow() - timedelta(minutes=1)))
        first = run(FAKE.claim_schedule(sid))
        second = run(FAKE.claim_schedule(sid))
        self.assertTrue(first)
        self.assertFalse(second, "a second worker must not be able to claim the same row")

    def test_due_query_skips_claimed_rows(self):
        sid = run(FAKE.create_schedule(7, 1, datetime.utcnow() - timedelta(minutes=1)))
        run(FAKE.claim_schedule(sid))
        self.assertEqual(run(FAKE.get_due_schedules()), [],
                         "a claimed row must not come back as due")

    def test_publish_runs_once_across_two_overlapping_ticks(self):
        pid = run(FAKE.save_post(7, "text", text="hi",
                                 target_channels_json="[1]", delivery_status="scheduled"))
        run(FAKE.create_schedule(7, pid, datetime.utcnow() - timedelta(minutes=1)))

        calls = []

        async def fake_publish(p, bot, only_channel_ids=None, attempt_no=1):
            calls.append(p["id"])
            await asyncio.sleep(0.05)
            return 1, 0

        original = post.publish_existing_post
        post.publish_existing_post = fake_publish
        try:
            async def both():
                ctx = make_context()
                await asyncio.gather(
                    post.process_scheduled_posts(ctx),
                    post.process_scheduled_posts(ctx),
                )
            run(both())
        finally:
            post.publish_existing_post = original

        self.assertEqual(len(calls), 1,
                         "overlapping ticks published the same post twice")


class ExpiryTests(SchedulingTestCase):
    """P0 #4 — a long outage must not flush every missed post at once."""

    def test_overdue_schedule_expires_instead_of_publishing(self):
        pid = run(FAKE.save_post(7, "text", text="old",
                                 target_channels_json="[1]", delivery_status="scheduled"))
        run(FAKE.create_schedule(7, pid, datetime.utcnow() - timedelta(days=2)))

        calls = []

        async def fake_publish(p, bot, only_channel_ids=None, attempt_no=1):
            calls.append(p["id"])
            return 1, 0

        original = post.publish_existing_post
        post.publish_existing_post = fake_publish
        try:
            bot = FakeBot()
            run(post.process_scheduled_posts(make_context(bot)))
        finally:
            post.publish_existing_post = original

        self.assertEqual(calls, [], "a two-day-old schedule must not publish")
        statuses = [s["status"] for s in FAKE.schedules.values()]
        self.assertIn("expired", statuses)
        self.assertTrue(bot.sent, "the author must be told the schedule was missed")


class ApprovalBypassTests(SchedulingTestCase):
    """P0 #2 — scheduling must not be a way around approval."""

    def test_writer_post_goes_to_approval_at_fire_time(self):
        FAKE.settings["approval_required"] = "1"
        FAKE.roles[7] = "writer"
        pid = run(FAKE.save_post(7, "text", text="hi",
                                 target_channels_json="[1]", delivery_status="scheduled"))
        run(FAKE.create_schedule(7, pid, datetime.utcnow() - timedelta(minutes=1)))

        calls = []

        async def fake_publish(p, bot, only_channel_ids=None, attempt_no=1):
            calls.append(p["id"])
            return 1, 0

        original = post.publish_existing_post
        post.publish_existing_post = fake_publish
        try:
            run(post.process_scheduled_posts(make_context()))
        finally:
            post.publish_existing_post = original

        self.assertEqual(calls, [], "approval was bypassed by scheduling")
        self.assertEqual(FAKE.posts[pid]["delivery_status"], "pending_approval")

    def test_deauthorised_author_schedule_is_cancelled(self):
        FAKE.roles[7] = "writer"
        pid = run(FAKE.save_post(7, "text", text="hi",
                                 target_channels_json="[1]", delivery_status="scheduled"))
        sid = run(FAKE.create_schedule(7, pid, datetime.utcnow() - timedelta(minutes=1)))

        async def deny(user_id):
            return False

        calls = []

        async def fake_publish(p, bot, only_channel_ids=None, attempt_no=1):
            calls.append(p["id"])
            return 1, 0

        # handlers.post binds these names at import time.
        original = post.publish_existing_post
        original_auth = post.is_writer_or_above
        post.publish_existing_post = fake_publish
        post.is_writer_or_above = deny
        try:
            run(post.process_scheduled_posts(make_context()))
        finally:
            post.publish_existing_post = original
            post.is_writer_or_above = original_auth

        self.assertEqual(calls, [], "a demoted author's post was still published")
        self.assertEqual(FAKE.schedules[sid]["status"], "cancelled")


class RetryScheduleTests(SchedulingTestCase):
    """The 1h / 3h / 6h ladder must advance and then stop."""

    def test_delays_follow_1_3_6_then_stop(self):
        base = datetime.utcnow()
        first = post._next_retry_at(1)
        second = post._next_retry_at(2)
        third = post._next_retry_at(3)
        self.assertAlmostEqual((first - base).total_seconds() / 3600, 1, delta=0.05)
        self.assertAlmostEqual((second - base).total_seconds() / 3600, 3, delta=0.05)
        self.assertAlmostEqual((third - base).total_seconds() / 3600, 6, delta=0.05)
        self.assertIsNone(post._next_retry_at(4),
                          "retries must stop after the configured ladder")

    def test_exhausted_retry_is_not_rearmed(self):
        self.assertIsNone(post._next_retry_at(99))


class RetryTargetingTests(SchedulingTestCase):
    """A retry must touch only the channels that failed."""

    def test_retry_only_resends_failed_channel(self):
        pid = run(FAKE.save_post(7, "text", text="hi", target_channels_json="[1, 2]"))
        run(FAKE.record_delivery(pid, 1, "telegram", "completed"))
        run(FAKE.record_delivery(pid, 2, "telegram", "failed", "boom",
                                 datetime.utcnow() - timedelta(minutes=1)))

        seen = {}

        async def fake_publish(p, bot, only_channel_ids=None, attempt_no=1):
            seen["ids"] = only_channel_ids
            return 1, 0

        original = post.publish_existing_post
        post.publish_existing_post = fake_publish
        try:
            run(post.process_delivery_retries(make_context()))
        finally:
            post.publish_existing_post = original

        self.assertEqual(seen.get("ids"), {2},
                         "retry must not re-send to channels that already succeeded")

    def test_due_retry_is_claimed_once(self):
        pid = run(FAKE.save_post(7, "text", text="hi", target_channels_json="[1]"))
        run(FAKE.record_delivery(pid, 1, "telegram", "failed", "boom",
                                 datetime.utcnow() - timedelta(minutes=1)))
        due = run(FAKE.get_due_retries())
        self.assertEqual(len(due), 1)
        self.assertTrue(run(FAKE.claim_delivery_retry(due[0]["id"])))
        self.assertFalse(run(FAKE.claim_delivery_retry(due[0]["id"])),
                         "the same retry must not be claimed twice")


class EmptyTargetTests(SchedulingTestCase):
    """P1 #11 — an empty target list means no channels, not all channels."""

    def test_empty_selection_does_not_broadcast(self):
        post_row = {"id": 1, "post_type": "text", "text": "hi",
                    "target_channels_json": "[]", "media_json": None}
        tg, bale = run(post._resolve_targets(post_row))
        self.assertEqual(tg, [])
        self.assertEqual(bale, [])

    def test_missing_target_column_falls_back_to_all(self):
        post_row = {"id": 1, "post_type": "text", "text": "hi",
                    "target_channels_json": None, "media_json": None}
        tg, bale = run(post._resolve_targets(post_row))
        self.assertEqual(len(tg), 2, "a legacy row with no targets should still publish")


class TimezoneTests(SchedulingTestCase):
    """P1 #14 — one time base, converted at the edges."""

    def test_local_to_utc_and_back_roundtrips(self):
        local = now_local().replace(microsecond=0) + timedelta(hours=3)
        stored = local_to_utc_naive(local)
        self.assertIsNone(stored.tzinfo, "stored times must be naive UTC")
        back = utc_naive_to_local(stored)
        self.assertEqual(back.replace(tzinfo=None), local.replace(tzinfo=None))

    def test_naive_input_is_treated_as_local(self):
        naive = (now_local() + timedelta(hours=2)).replace(tzinfo=None, microsecond=0)
        stored = local_to_utc_naive(naive)
        self.assertEqual(utc_naive_to_local(stored).replace(tzinfo=None), naive)


class StaleButtonTests(SchedulingTestCase):
    """P1 #5 — a stale wizard button must not crash or hijack a new post."""

    def _query(self, data, user_id=7):
        answered = []
        edits = []

        async def answer(text=None, show_alert=False):
            answered.append(text)

        async def edit_message_text(text, **kw):
            edits.append(text)

        return types.SimpleNamespace(
            data=data, from_user=types.SimpleNamespace(id=user_id),
            answer=answer, edit_message_text=edit_message_text,
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=user_id)),
            _answered=answered, _edits=edits,
        )

    def test_stale_minute_button_does_not_raise_keyerror(self):
        # A brand-new post is being composed; the old minute button belongs to
        # a finished schedule and must be rejected, not crash on schedule_date.
        post.user_states[7] = {"state": "awaiting_post", "created_at": time.monotonic()}
        query = self._query("schedule_minute_2030-01-01_10_30")
        update = types.SimpleNamespace(callback_query=query, message=None)
        run(post.handle_schedule_minute(update, make_context()))
        self.assertIn(post._STALE_BUTTON_MSG, query._answered)
        self.assertEqual(post.user_states[7]["state"], "awaiting_post",
                         "a stale button must not mutate the current workflow")

    def test_stale_hour_button_does_not_hijack_current_post(self):
        post.user_states[7] = {"state": "awaiting_post", "created_at": time.monotonic()}
        query = self._query("schedule_hour_2030-01-01_10")
        update = types.SimpleNamespace(callback_query=query, message=None)
        run(post.handle_schedule_hour(update, make_context()))
        self.assertEqual(post.user_states[7]["state"], "awaiting_post")

    def test_past_date_button_is_refused(self):
        post.user_states[7] = {"state": "awaiting_schedule_date", "created_at": time.monotonic()}
        past = (now_local().date() - timedelta(days=1)).isoformat()
        query = self._query(f"schedule_date_{past}")
        update = types.SimpleNamespace(callback_query=query, message=None)
        run(post.handle_schedule_date(update, make_context()))
        self.assertTrue(any(a and "گذشته" in a for a in query._answered))


class SchedulePastErrorTests(SchedulingTestCase):
    """P1 #7 — only a real past time may be reported as 'past'."""

    def test_schedule_past_error_is_distinct_from_valueerror(self):
        self.assertTrue(issubclass(post.SchedulePastError, ValueError))
        self.assertIsNot(post.SchedulePastError, ValueError)

    def test_finish_schedule_raises_typed_error_for_past_time(self):
        state = {"state": "awaiting_schedule_minute", "type": "text", "text": "x",
                 "selected_channel_ids": [1], "created_at": time.monotonic()}
        past = (now_local() - timedelta(hours=1)).replace(tzinfo=None)
        with self.assertRaises(post.SchedulePastError):
            run(post._finish_schedule(7, state, past, types.SimpleNamespace(
                callback_query=None, message=None), make_context()))


class ScheduledStatusTests(SchedulingTestCase):
    """P0 #1 — a scheduled post must not be publishable as a draft."""

    def test_finish_schedule_marks_post_scheduled_not_draft(self):
        state = {"state": "awaiting_schedule_minute", "type": "text", "text": "x",
                 "selected_channel_ids": [1], "created_at": time.monotonic()}
        future = (now_local() + timedelta(hours=2)).replace(tzinfo=None, microsecond=0)

        edits = []

        async def edit_message_text(text, **kw):
            edits.append(text)

        update = types.SimpleNamespace(
            callback_query=types.SimpleNamespace(edit_message_text=edit_message_text),
            message=None,
        )
        run(post._finish_schedule(7, state, future, update, make_context()))

        created = list(FAKE.posts.values())[-1]
        self.assertEqual(created["delivery_status"], "scheduled",
                         "a scheduled post stored as 'draft' can be published twice")

    def test_scheduled_post_detail_keyboard_has_no_publish_button(self):
        from keyboards import post_detail_keyboard
        markup = post_detail_keyboard(5, "scheduled", schedule_id=9)
        payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertNotIn("publish_draft_5", payloads,
                         "a scheduled post must not offer 'publish draft'")
        self.assertIn("sched_view_9", payloads)


class WorkflowPersistenceTests(SchedulingTestCase):
    """The restart-safety feature: composing work must survive a restart."""

    def test_state_is_persisted_and_restored(self):
        state = {"state": "awaiting_confirm", "type": "text", "text": "draft text",
                 "selected_channel_ids": [1], "created_at": time.monotonic()}
        run(post.persist_state(7, state))
        self.assertIn(7, FAKE.sessions)

        # Simulate the process dying and coming back up.
        post.user_states.clear()
        run(post.restore_workflow_states(make_context()))
        self.assertIn(7, post.user_states)
        self.assertEqual(post.user_states[7]["text"], "draft text")
        self.assertTrue(post.user_states[7]["restored"])

    def test_unserialisable_message_object_is_dropped(self):
        state = {"state": "awaiting_confirm", "type": "text", "text": "x",
                 "message": object(), "created_at": time.monotonic()}
        run(post.persist_state(7, state))
        import json
        payload = json.loads(FAKE.sessions[7]["payload"])
        self.assertNotIn("message", payload,
                         "Telegram objects must not break session persistence")

    def test_restored_media_group_state_is_downgraded(self):
        # The album debounce job dies with the process, so a restored
        # awaiting_media_group state would wait forever.
        state = {"state": "awaiting_media_group", "media": [], "created_at": time.monotonic()}
        run(post.persist_state(7, state))
        post.user_states.clear()
        run(post.restore_workflow_states(make_context()))
        self.assertEqual(post.user_states[7]["state"], "awaiting_post")

    def test_forget_state_clears_both_copies(self):
        state = {"state": "awaiting_confirm", "created_at": time.monotonic()}
        post.user_states[7] = state
        run(post.persist_state(7, state))
        run(post.forget_state(7))
        self.assertNotIn(7, post.user_states)
        self.assertNotIn(7, FAKE.sessions)


class InflightTrackingTests(SchedulingTestCase):
    """A restart must be able to wait for publishes that are mid-flight."""

    def test_wait_for_inflight_returns_true_when_idle(self):
        self.assertTrue(run(post.wait_for_inflight(1)))

    def test_inflight_is_tracked_during_publish(self):
        observed = []

        async def slow_send(channels, state, bot):
            observed.append(run_state_snapshot())
            return len(channels), 0, [], {}

        def run_state_snapshot():
            return set(post._inflight_publishes)

        original_tg = post._post_to_telegram
        original_bale = post._post_to_bale
        post._post_to_telegram = slow_send

        async def no_bale(channels, state, bot):
            return 0, 0, [], {}
        post._post_to_bale = no_bale
        try:
            row = {"id": 42, "post_type": "text", "text": "hi",
                   "target_channels_json": "[1]", "media_json": None,
                   "tg_message_ids": None}
            run(post.publish_existing_post(row, FakeBot()))
        finally:
            post._post_to_telegram = original_tg
            post._post_to_bale = original_bale

        self.assertIn(42, observed[0], "publish must register itself as in-flight")
        self.assertNotIn(42, post._inflight_publishes,
                         "in-flight set must be cleaned up afterwards")


class ScheduleManagementTests(SchedulingTestCase):
    """P2 #15 — schedules must be listable and cancellable."""

    def test_cancel_removes_from_pending_list(self):
        sid = run(FAKE.create_schedule(7, 1, datetime.utcnow() + timedelta(hours=5)))
        self.assertEqual(len(run(FAKE.get_pending_schedules(7))), 1)
        self.assertTrue(run(FAKE.cancel_schedule(sid)))
        self.assertEqual(run(FAKE.get_pending_schedules(7)), [])

    def test_cannot_cancel_a_claimed_schedule(self):
        sid = run(FAKE.create_schedule(7, 1, datetime.utcnow() - timedelta(minutes=1)))
        run(FAKE.claim_schedule(sid))
        self.assertFalse(run(FAKE.cancel_schedule(sid)),
                         "cancelling an in-flight publish would half-send the post")

    def test_reschedule_moves_run_at(self):
        sid = run(FAKE.create_schedule(7, 1, datetime.utcnow() + timedelta(hours=1)))
        new_time = datetime.utcnow() + timedelta(hours=9)
        self.assertTrue(run(FAKE.reschedule(sid, new_time)))
        self.assertEqual(FAKE.schedules[sid]["run_at"], new_time)

    def test_cannot_reschedule_a_claimed_row(self):
        sid = run(FAKE.create_schedule(7, 1, datetime.utcnow() - timedelta(minutes=1)))
        run(FAKE.claim_schedule(sid))
        self.assertFalse(run(FAKE.reschedule(sid, datetime.utcnow() + timedelta(hours=2))))


class KeyboardTests(SchedulingTestCase):
    """P2 #17 — the wizard must not offer times that already passed."""

    def test_hour_keyboard_hides_past_hours(self):
        from keyboards import schedule_hour_keyboard
        markup = schedule_hour_keyboard("2030-01-01", min_hour=20)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertNotIn("19", labels)
        self.assertIn("20", labels)

    def test_minute_keyboard_respects_allowed_minutes(self):
        from keyboards import schedule_minute_keyboard
        markup = schedule_minute_keyboard("2030-01-01", 10, allowed_minutes=[45, 50, 55])
        payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertTrue(any(p.endswith("_10_45") for p in payloads))
        self.assertFalse(any(p.endswith("_10_00") for p in payloads))

    def test_date_keyboard_offers_a_week(self):
        from keyboards import schedule_date_keyboard
        markup = schedule_date_keyboard(datetime(2030, 1, 1).date())
        payloads = [b.callback_data for row in markup.inline_keyboard
                    for b in row if b.callback_data.startswith("schedule_date_")]
        self.assertEqual(len(payloads), 7, "only today/tomorrow were selectable before")
        self.assertIn("schedule_date_2030-01-07", payloads)

    def test_date_payload_is_unambiguous_iso(self):
        from keyboards import schedule_date_keyboard
        markup = schedule_date_keyboard(datetime(2030, 5, 9).date())
        first = markup.inline_keyboard[0][0].callback_data
        self.assertEqual(first, "schedule_date_2030-05-09")


if __name__ == "__main__":
    unittest.main()


class RetryLadderGroupingTests(SchedulingTestCase):
    """Channels on different rungs of the ladder must not be merged."""

    def test_channels_with_different_attempts_are_sent_separately(self):
        pid = run(FAKE.save_post(7, "text", text="hi", target_channels_json="[1, 2]"))
        overdue = datetime.utcnow() - timedelta(minutes=1)
        # Channel 1 has failed once; channel 2 has failed three times.
        run(FAKE.record_delivery(pid, 1, "telegram", "failed", "e", overdue))
        for _ in range(3):
            run(FAKE.record_delivery(pid, 2, "telegram", "failed", "e", overdue))

        batches = []

        async def fake_publish(p, bot, only_channel_ids=None, attempt_no=1):
            batches.append((frozenset(only_channel_ids), attempt_no))
            return 1, 0

        original = post.publish_existing_post
        post.publish_existing_post = fake_publish
        try:
            run(post.process_delivery_retries(make_context()))
        finally:
            post.publish_existing_post = original

        self.assertEqual(len(batches), 2,
                         "channels on different retry rungs were merged into one batch")
        by_channel = {next(iter(ids)): attempt for ids, attempt in batches}
        self.assertLess(by_channel[1], by_channel[2],
                        "a first-time failure must not inherit an older channel's attempt count")


class PartialRetryPreservationTests(SchedulingTestCase):
    """A partial retry must not disturb channels that already succeeded."""

    def test_successful_channel_row_is_untouched_by_partial_retry(self):
        pid = run(FAKE.save_post(7, "text", text="hi", target_channels_json="[1, 2]"))
        run(FAKE.record_delivery(pid, 1, "telegram", "completed"))
        run(FAKE.record_delivery(pid, 2, "telegram", "failed", "boom",
                                 datetime.utcnow() - timedelta(minutes=1)))
        before = dict(FAKE.deliveries[(pid, 1)])

        async def only_tg(channels, state, bot):
            return len(channels), 0, [], {}

        async def no_bale(channels, state, bot):
            return 0, 0, [], {}

        o1, o2 = post._post_to_telegram, post._post_to_bale
        post._post_to_telegram, post._post_to_bale = only_tg, no_bale
        try:
            row = run(FAKE.get_post(pid))
            run(post.publish_existing_post(row, FakeBot(), only_channel_ids={2}, attempt_no=2))
        finally:
            post._post_to_telegram, post._post_to_bale = o1, o2

        after = FAKE.deliveries[(pid, 1)]
        self.assertEqual(after["attempts"], before["attempts"],
                         "a partial retry re-touched a channel that had already succeeded")
        self.assertEqual(after["status"], "completed")
