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
            "tg_message_ids": None, "created_at": datetime.utcnow(),
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

    async def cancel_post_retries(self, post_id):
        cleared = 0
        for r in self.deliveries.values():
            if r["post_id"] != post_id:
                continue
            if r.get("status") == "failed" and r.get("next_retry_at") is not None:
                r["next_retry_at"] = None
                cleared += 1
            elif r.get("status") == "retrying":
                r["status"] = "failed"
                r["next_retry_at"] = None
        return cleared

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

    async def can_edit_post(self, user_id, post_id):
        role = self.roles.get(user_id, "writer")
        if role in ("sudo", "owner"):
            return True
        post = self.posts.get(post_id)
        return bool(post and post["user_id"] == user_id)

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
    import handlers.history as history
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
    "claim_delivery_retry", "cancel_post_retries", "reclaim_stale_retries",
    "get_post_deliveries",
    "save_workflow_session", "load_workflow_sessions", "delete_workflow_session",
    "purge_workflow_sessions", "get_setting", "get_active_channels",
    "is_writer_or_above", "has_permission", "get_user_role", "is_sudo", "is_owner",
    "can_edit_post",
)


def _rebind_fakes():
    for module in (post, schedules, history):
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
    """Retries now fire on a fixed 10-minute cadence and never give up."""

    def test_next_retry_is_fixed_10_minutes(self):
        base = datetime.utcnow()
        nxt = post._next_retry_at()
        self.assertAlmostEqual((nxt - base).total_seconds() / 60, 10, delta=0.05)

    def test_retry_never_exhausts(self):
        # No matter how many attempts have already failed, another retry is
        # always scheduled — success (or manual cancellation) is the only stop.
        for _ in range(50):
            self.assertIsNotNone(post._next_retry_at(),
                                 "retries must never be exhausted")

    def test_next_retry_independent_of_attempt_count(self):
        # The cadence is flat: attempt count must not change the delay.
        base = datetime.utcnow()
        a = post._next_retry_at()
        b = post._next_retry_at()
        self.assertAlmostEqual((a - base).total_seconds() / 60, 10, delta=0.1)
        self.assertAlmostEqual((b - base).total_seconds() / 60, 10, delta=0.1)


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

    def test_failed_retry_is_rearmed_for_10_minutes_later(self):
        # A failed retry must schedule the next attempt 10 minutes out, even
        # after many previous attempts (no exhaustion).
        pid = run(FAKE.save_post(7, "text", text="hi", target_channels_json="[1]"))
        for _ in range(20):
            run(FAKE.record_delivery(pid, 1, "telegram", "failed", "boom",
                                     datetime.utcnow() - timedelta(minutes=11)))

        async def failing_send(channels, state, bot):
            return 0, len(channels), [], {c["id"]: "still broken" for c in channels}

        async def no_bale(channels, state, bot, attempt_no=1):
            return 0, 0, [], {}

        o1, o2 = post._post_to_telegram, post._post_to_bale
        post._post_to_telegram, post._post_to_bale = failing_send, no_bale
        try:
            run(post.process_delivery_retries(make_context()))
        finally:
            post._post_to_telegram, post._post_to_bale = o1, o2

        row = FAKE.deliveries[(pid, 1)]
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["next_retry_at"],
                             "a failed retry must always be re-armed, never given up")
        delta = (row["next_retry_at"] - datetime.utcnow()).total_seconds() / 60
        self.assertAlmostEqual(delta, 10, delta=0.5)

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

        async def no_bale(channels, state, bot, attempt_no=1):
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
        # Labels are localised, so assert on the payloads, which are stable.
        payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertNotIn("schedule_hour_2030-01-01_19", payloads)
        self.assertIn("schedule_hour_2030-01-01_20", payloads)
        self.assertIn("schedule_hour_2030-01-01_23", payloads)

    def test_hour_keyboard_labels_follow_the_display_calendar(self):
        import utils
        from keyboards import schedule_hour_keyboard
        original = utils.USE_JALALI
        try:
            utils.USE_JALALI = True
            labels = [b.text for row in schedule_hour_keyboard("2030-01-01", 20).inline_keyboard
                      for b in row]
            self.assertIn("۲۰", labels)
            utils.USE_JALALI = False
            labels = [b.text for row in schedule_hour_keyboard("2030-01-01", 20).inline_keyboard
                      for b in row]
            self.assertIn("20", labels)
        finally:
            utils.USE_JALALI = original

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


class RetryGroupingTests(SchedulingTestCase):
    """Due channels batch per post — and per Bale-bot parity."""

    def test_channels_with_same_attempt_parity_are_merged(self):
        pid = run(FAKE.save_post(7, "text", text="hi", target_channels_json="[1, 2]"))
        overdue = datetime.utcnow() - timedelta(minutes=1)
        # Channel 1 has failed once; channel 2 has failed three times. Both
        # counts are odd, so both next attempts fall on the same Bale bot and
        # must go out in a single publish.
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

        self.assertEqual(len(batches), 1)
        ids, attempt_no = batches[0]
        self.assertEqual(ids, frozenset({1, 2}))
        self.assertEqual(attempt_no % 2, 0,
                         "odd attempt counts mean the next send is an even attempt (bot 2)")

    def test_channels_with_different_attempt_parity_are_split(self):
        # Bale attempts alternate bots, so channels whose next attempt lands
        # on different bots must not share a publish.
        pid = run(FAKE.save_post(7, "text", text="hi", target_channels_json="[1, 2]"))
        overdue = datetime.utcnow() - timedelta(minutes=1)
        # Channel 1 failed once (next attempt even -> bot 2);
        # channel 2 failed twice (next attempt odd -> bot 1).
        run(FAKE.record_delivery(pid, 1, "telegram", "failed", "e", overdue))
        for _ in range(2):
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
                         "channels on different bots were merged into one batch")
        by_ids = {ids: attempt_no for ids, attempt_no in batches}
        self.assertEqual(by_ids[frozenset({1})] % 2, 0, "channel 1 needs the bot-2 attempt")
        self.assertEqual(by_ids[frozenset({2})] % 2, 1, "channel 2 needs the bot-1 attempt")


class BaleBotAlternationTests(SchedulingTestCase):
    """Attempts alternate between the primary Bale bot and the backup."""

    def test_odd_attempts_use_bot1_even_use_bot2(self):
        import bale_client
        fake_backup = object()
        original = bale_client.BACKUP_CLIENT
        bale_client.BACKUP_CLIENT = fake_backup
        try:
            self.assertIs(bale_client.client_for_attempt(1), bale_client.DEFAULT_CLIENT)
            self.assertIs(bale_client.client_for_attempt(2), fake_backup)
            self.assertIs(bale_client.client_for_attempt(3), bale_client.DEFAULT_CLIENT)
            self.assertIs(bale_client.client_for_attempt(4), fake_backup)
        finally:
            bale_client.BACKUP_CLIENT = original

    def test_without_backup_every_attempt_uses_bot1(self):
        import bale_client
        original = bale_client.BACKUP_CLIENT
        bale_client.BACKUP_CLIENT = None
        try:
            for attempt in range(1, 6):
                self.assertIs(bale_client.client_for_attempt(attempt),
                              bale_client.DEFAULT_CLIENT)
        finally:
            bale_client.BACKUP_CLIENT = original

    def test_post_to_bale_sends_through_the_attempt_bot(self):
        import bale_client
        used = []

        class FakeClient:
            def __init__(self, name):
                self.name = name

            async def send_message(self, chat_id, text, **kw):
                used.append(self.name)
                return {"ok": True, "result": {"message_id": 1}}

        def pick(attempt_no):
            return FakeClient("bot1" if attempt_no % 2 else "bot2")

        original = bale_client.client_for_attempt
        bale_client.client_for_attempt = pick
        try:
            chans = [{"id": 1, "chat_id": -100, "name": "B", "platform": "bale"}]
            state = {"type": "text", "text": "hi"}
            run(post._post_to_bale(chans, state, None, attempt_no=1))
            run(post._post_to_bale(chans, state, None, attempt_no=2))
            run(post._post_to_bale(chans, state, None, attempt_no=3))
        finally:
            bale_client.client_for_attempt = original

        self.assertEqual(used, ["bot1", "bot2", "bot1"],
                         "attempts must alternate bots, starting with bot 1")

    def test_retry_now_splits_batches_by_bot(self):
        FAKE.roles[1] = "owner"
        pid = run(FAKE.save_post(7, "text", text="hi", target_channels_json="[1, 2]"))
        # ch1 failed once -> next attempt even -> bot 2;
        # ch2 failed twice -> next attempt odd -> bot 1.
        run(FAKE.record_delivery(pid, 1, "bale", "failed", "e",
                                 datetime.utcnow() + timedelta(minutes=10)))
        for _ in range(2):
            run(FAKE.record_delivery(pid, 2, "bale", "failed", "e",
                                     datetime.utcnow() + timedelta(minutes=10)))

        batches = []
        original_publish = post.publish_existing_post

        async def fake_publish(p, bot, only_channel_ids=None, attempt_no=1):
            batches.append((frozenset(only_channel_ids), attempt_no))
            return len(only_channel_ids), 0

        # history.py binds publish_existing_post at import time.
        original_bound = history.publish_existing_post
        post.publish_existing_post = fake_publish
        history.publish_existing_post = fake_publish
        try:
            query = RetryCancelButtonTests()._query(f"retry_now_{pid}")
            update = types.SimpleNamespace(callback_query=query)
            run(history.handle_retry_now(update, make_context()))
        finally:
            post.publish_existing_post = original_publish
            history.publish_existing_post = original_bound

        self.assertEqual(len(batches), 2,
                         "retry-now must publish one batch per bot")
        by_ids = {ids: attempt_no for ids, attempt_no in batches}
        self.assertEqual(by_ids[frozenset({1})] % 2, 0)
        self.assertEqual(by_ids[frozenset({2})] % 2, 1)


class BaleUploadOptimizationTests(SchedulingTestCase):
    """Media is downloaded once per post and channels upload in parallel."""

    def test_each_file_downloaded_once_across_many_channels(self):
        import io as _io

        downloads = []

        class FakeFile:
            async def download_to_memory(self, out=None):
                downloads.append(1)
                buf = out if out is not None else _io.BytesIO()
                buf.write(b"fake-media-bytes")
                buf.seek(0)
                return buf

        class FakeBot:
            async def get_file(self, file_id):
                return FakeFile()

        class FakeClient:
            name = "bale-test"

            def __init__(self):
                self.calls = []

            async def send_media_group(self, chat_id, media_files, caption=None):
                self.calls.append((chat_id, len(media_files)))
                return {"ok": True, "result": [{"message_id": 1}, {"message_id": 2}]}

        import bale_client
        client = FakeClient()
        original = bale_client.client_for_attempt
        bale_client.client_for_attempt = lambda attempt_no: client
        try:
            state = {"type": "media_group", "caption": "c",
                     "media": [{"type": "photo", "file_id": "a"},
                               {"type": "video", "file_id": "b"}]}
            channels = [
                {"id": 1, "chat_id": -100, "name": "B1", "platform": "bale"},
                {"id": 2, "chat_id": -200, "name": "B2", "platform": "bale"},
                {"id": 3, "chat_id": -300, "name": "B3", "platform": "bale"},
            ]
            sent, failed, message_ids, errors = run(
                post._post_to_bale(channels, state, FakeBot(), attempt_no=1))
        finally:
            bale_client.client_for_attempt = original

        self.assertEqual(sent, 3)
        self.assertEqual(failed, 0)
        self.assertEqual(len(downloads), 2,
                         "a 2-file album to 3 channels must download 2 files, not 6")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(len(message_ids), 6, "two messages per channel recorded")

    def test_prepare_failure_marks_every_channel_failed(self):
        class BrokenBot:
            async def get_file(self, file_id):
                raise RuntimeError("file too big")

        state = {"type": "photo", "file_id": "x", "caption": None}
        channels = [{"id": 1, "chat_id": -100, "name": "B1", "platform": "bale"}]
        sent, failed, message_ids, errors = run(
            post._post_to_bale(channels, state, BrokenBot(), attempt_no=1))
        self.assertEqual((sent, failed), (0, 1))
        self.assertIn("file too big", errors[1])


class RetryCancelButtonTests(SchedulingTestCase):
    """Cancel/retry-now: available to sudo/owner and to writers for their own posts."""

    def _query(self, data, user_id=1):
        answered = []
        edits = []
        replies = []

        async def answer(text=None, show_alert=False):
            answered.append((text, show_alert))

        async def edit_message_text(text, **kw):
            edits.append(text)

        async def reply_text(text, **kw):
            replies.append(text)

        query = types.SimpleNamespace(
            data=data, from_user=types.SimpleNamespace(id=user_id),
            answer=answer, edit_message_text=edit_message_text,
            message=types.SimpleNamespace(
                chat=types.SimpleNamespace(id=user_id), reply_text=reply_text,
            ),
        )
        query._answered = answered
        query._edits = edits
        query._replies = replies
        return query

    def _failed_post(self):
        """Post authored by user 7: one delivered channel, one failed (armed)."""
        pid = run(FAKE.save_post(7, "text", text="hi", target_channels_json="[1, 2]"))
        run(FAKE.record_delivery(pid, 1, "telegram", "completed"))
        run(FAKE.record_delivery(pid, 2, "telegram", "failed", "boom",
                                 datetime.utcnow() + timedelta(minutes=10)))
        return pid

    def test_cancel_clears_queue_and_accepts_incomplete_status(self):
        FAKE.roles[1] = "owner"
        pid = self._failed_post()
        query = self._query(f"cancel_retries_{pid}")
        update = types.SimpleNamespace(callback_query=query)
        run(history.handle_cancel_retries(update, make_context()))

        self.assertIsNone(FAKE.deliveries[(pid, 2)]["next_retry_at"],
                          "cancelled retries must never fire again")
        self.assertEqual(FAKE.deliveries[(pid, 2)]["status"], "failed")
        self.assertEqual(FAKE.posts[pid]["delivery_status"], "partial",
                         "the final status must be accepted as incomplete")
        self.assertEqual(run(FAKE.get_due_retries()), [])
        self.assertTrue(any("متوقف شد" in e for e in query._edits))

    def test_cancel_finalises_in_flight_rows_so_recovery_cannot_rearm(self):
        FAKE.roles[1] = "owner"
        pid = self._failed_post()
        # Claim the retry like the job would, then cancel mid-flight.
        run(FAKE.claim_delivery_retry(FAKE.deliveries[(pid, 2)]["id"]))
        self.assertEqual(FAKE.deliveries[(pid, 2)]["status"], "retrying")
        query = self._query(f"cancel_retries_{pid}")
        update = types.SimpleNamespace(callback_query=query)
        run(history.handle_cancel_retries(update, make_context()))
        row = FAKE.deliveries[(pid, 2)]
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["next_retry_at"])

    def test_writer_can_cancel_own_post(self):
        FAKE.roles[7] = "writer"
        pid = self._failed_post()
        query = self._query(f"cancel_retries_{pid}", user_id=7)
        update = types.SimpleNamespace(callback_query=query)
        run(history.handle_cancel_retries(update, make_context()))
        self.assertIsNone(FAKE.deliveries[(pid, 2)]["next_retry_at"],
                          "a writer must be able to stop retries of their own post")
        self.assertEqual(FAKE.posts[pid]["delivery_status"], "partial")

    def test_cancel_is_forbidden_for_other_peoples_posts(self):
        FAKE.roles[1] = "writer"
        pid = self._failed_post()
        query = self._query(f"cancel_retries_{pid}", user_id=1)
        update = types.SimpleNamespace(callback_query=query)
        run(history.handle_cancel_retries(update, make_context()))
        self.assertIsNotNone(FAKE.deliveries[(pid, 2)]["next_retry_at"],
                             "a writer must not cancel retries of another user's post")
        self.assertTrue(any(t and "اجازه" in t for t, _ in query._answered))

    def test_cancel_with_nothing_queued_leaves_status_alone(self):
        # A stale button on a healthy post must not flip its status.
        FAKE.roles[1] = "owner"
        pid = run(FAKE.save_post(7, "text", text="hi", target_channels_json="[1]",
                                 delivery_status="completed"))
        run(FAKE.record_delivery(pid, 1, "telegram", "completed"))
        query = self._query(f"cancel_retries_{pid}")
        update = types.SimpleNamespace(callback_query=query)
        run(history.handle_cancel_retries(update, make_context()))
        self.assertEqual(FAKE.posts[pid]["delivery_status"], "completed",
                         "cancelling with an empty queue must not touch the status")
        self.assertTrue(any(t and "در صف نیست" in t for t, _ in query._answered))

    def test_retry_now_success_marks_post_completed(self):
        FAKE.roles[1] = "owner"
        pid = self._failed_post()
        # Clear the armed retry: retry-now must work even after cancellation.
        run(FAKE.cancel_post_retries(pid))

        async def only_tg(channels, state, bot):
            return len(channels), 0, [], {}

        async def no_bale(channels, state, bot, attempt_no=1):
            return 0, 0, [], {}

        o1, o2 = post._post_to_telegram, post._post_to_bale
        post._post_to_telegram, post._post_to_bale = only_tg, no_bale
        try:
            query = self._query(f"retry_now_{pid}")
            update = types.SimpleNamespace(callback_query=query)
            run(history.handle_retry_now(update, make_context()))
        finally:
            post._post_to_telegram, post._post_to_bale = o1, o2

        self.assertEqual(FAKE.deliveries[(pid, 2)]["status"], "completed")
        self.assertEqual(FAKE.posts[pid]["delivery_status"], "completed",
                         "a successful retry-now must mark the post complete")
        self.assertTrue(any("کامل شد" in r for r in query._replies))

    def test_retry_now_failure_arms_next_attempt_in_10_minutes(self):
        FAKE.roles[1] = "owner"
        pid = self._failed_post()
        run(FAKE.cancel_post_retries(pid))

        async def failing_send(channels, state, bot):
            return 0, len(channels), [], {c["id"]: "still broken" for c in channels}

        async def no_bale(channels, state, bot, attempt_no=1):
            return 0, 0, [], {}

        o1, o2 = post._post_to_telegram, post._post_to_bale
        post._post_to_telegram, post._post_to_bale = failing_send, no_bale
        try:
            query = self._query(f"retry_now_{pid}")
            update = types.SimpleNamespace(callback_query=query)
            run(history.handle_retry_now(update, make_context()))
        finally:
            post._post_to_telegram, post._post_to_bale = o1, o2

        row = FAKE.deliveries[(pid, 2)]
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["next_retry_at"],
                             "a failed retry-now must arm the next attempt")
        delta = (row["next_retry_at"] - datetime.utcnow()).total_seconds() / 60
        self.assertAlmostEqual(delta, 10, delta=0.5,
                               msg="the next attempt counts from the retry-now time")
        self.assertTrue(any("10" in r and "دقیقه" in r for r in query._replies))

    def test_writer_can_retry_now_own_post(self):
        FAKE.roles[7] = "writer"
        pid = self._failed_post()

        async def only_tg(channels, state, bot):
            return len(channels), 0, [], {}

        async def no_bale(channels, state, bot, attempt_no=1):
            return 0, 0, [], {}

        o1, o2 = post._post_to_telegram, post._post_to_bale
        post._post_to_telegram, post._post_to_bale = only_tg, no_bale
        try:
            query = self._query(f"retry_now_{pid}", user_id=7)
            update = types.SimpleNamespace(callback_query=query)
            run(history.handle_retry_now(update, make_context()))
        finally:
            post._post_to_telegram, post._post_to_bale = o1, o2

        self.assertEqual(FAKE.posts[pid]["delivery_status"], "completed",
                         "a writer must be able to complete their own post")

    def test_retry_now_is_forbidden_for_other_peoples_posts(self):
        FAKE.roles[1] = "writer"
        pid = self._failed_post()
        query = self._query(f"retry_now_{pid}", user_id=1)
        update = types.SimpleNamespace(callback_query=query)
        run(history.handle_retry_now(update, make_context()))
        self.assertTrue(any(t and "اجازه" in t for t, _ in query._answered))

    def test_retry_now_refuses_when_the_auto_job_is_already_sending(self):
        # The retry job claimed the row first: retry-now must back off instead
        # of double-sending the channel.
        FAKE.roles[1] = "owner"
        pid = self._failed_post()
        run(FAKE.claim_delivery_retry(FAKE.deliveries[(pid, 2)]["id"]))
        query = self._query(f"retry_now_{pid}")
        update = types.SimpleNamespace(callback_query=query)
        run(history.handle_retry_now(update, make_context()))
        self.assertTrue(any(t and "در حال ارسال" in t for t, _ in query._answered))
        self.assertEqual(query._replies, [],
                         "nothing may be sent when the auto job owns the rows")

    def test_retry_now_with_dead_channel_does_not_claim_complete(self):
        # The failed channel was removed afterwards: retry-now must finalise
        # it instead of claiming the post is complete with zero sends.
        FAKE.roles[1] = "owner"
        pid = self._failed_post()

        async def only_channel_1(platform=None):
            chans = [{"id": 1, "chat_id": -100, "name": "A",
                      "chat_type": "channel", "platform": "telegram"}]
            return [c for c in chans if platform is None or c["platform"] == platform]

        original = post.get_active_channels
        post.get_active_channels = only_channel_1
        try:
            query = self._query(f"retry_now_{pid}")
            update = types.SimpleNamespace(callback_query=query)
            run(history.handle_retry_now(update, make_context()))
        finally:
            post.get_active_channels = original

        self.assertEqual(FAKE.deliveries[(pid, 2)]["status"], "cancelled")
        self.assertTrue(any("در دسترس نیستند" in e for e in query._edits))
        self.assertFalse(any("کامل شد" in e for e in query._edits),
                         "a dead channel must not be reported as a completed post")


class DeadChannelRetryLoopTests(SchedulingTestCase):
    """A channel removed after a failure must not be retried forever."""

    def test_dead_channel_row_is_finalised_not_retried_forever(self):
        pid = run(FAKE.save_post(7, "text", text="hi", target_channels_json="[1, 2]"))
        overdue = datetime.utcnow() - timedelta(minutes=1)
        run(FAKE.record_delivery(pid, 1, "telegram", "failed", "e", overdue))
        run(FAKE.record_delivery(pid, 2, "telegram", "failed", "e", overdue))

        async def only_channel_1(platform=None):
            chans = [{"id": 1, "chat_id": -100, "name": "A",
                      "chat_type": "channel", "platform": "telegram"}]
            return [c for c in chans if platform is None or c["platform"] == platform]

        batches = []

        async def fake_publish(p, bot, only_channel_ids=None, attempt_no=1):
            batches.append(frozenset(only_channel_ids))
            return len(only_channel_ids), 0

        original_channels = post.get_active_channels
        original_publish = post.publish_existing_post
        post.get_active_channels = only_channel_1
        post.publish_existing_post = fake_publish
        try:
            run(post.process_delivery_retries(make_context()))
        finally:
            post.get_active_channels = original_channels
            post.publish_existing_post = original_publish

        self.assertEqual(batches, [frozenset({1})],
                         "only the live channel may be re-sent")
        dead_row = FAKE.deliveries[(pid, 2)]
        self.assertEqual(dead_row["status"], "cancelled")
        self.assertIsNone(dead_row["next_retry_at"])
        self.assertEqual(run(FAKE.get_due_retries()), [],
                         "a dead channel must never re-enter the retry queue")


class RetryButtonKeyboardTests(SchedulingTestCase):
    """The retry-management buttons are conditional on active retries."""

    def test_buttons_shown_while_retries_active(self):
        from keyboards import post_detail_keyboard
        markup = post_detail_keyboard(5, "partial", can_manage_retries=True,
                                      has_active_retries=True)
        payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("retry_now_5", payloads)
        self.assertIn("cancel_retries_5", payloads)

    def test_buttons_hidden_for_viewers_without_edit_rights(self):
        from keyboards import post_detail_keyboard
        markup = post_detail_keyboard(5, "partial", can_manage_retries=False,
                                      has_active_retries=True)
        payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertNotIn("retry_now_5", payloads)
        self.assertNotIn("cancel_retries_5", payloads)

    def test_buttons_hidden_when_post_fully_sent(self):
        from keyboards import post_detail_keyboard
        markup = post_detail_keyboard(5, "completed", can_manage_retries=True,
                                      has_active_retries=False)
        payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertNotIn("retry_now_5", payloads)
        self.assertNotIn("cancel_retries_5", payloads)

    def test_buttons_hidden_after_retries_cancelled(self):
        # Failed rows still exist, but nothing is queued anymore.
        from keyboards import post_detail_keyboard
        markup = post_detail_keyboard(5, "partial", can_manage_retries=True,
                                      has_active_retries=False)
        payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertNotIn("retry_now_5", payloads)
        self.assertNotIn("cancel_retries_5", payloads)


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

        async def no_bale(channels, state, bot, attempt_no=1):
            return 0, 0, [], {}

        o1, o2 = post._post_to_telegram, post._post_to_bale
        post._post_to_telegram, post._post_to_bale = only_tg, no_bale
        try:
            row = run(FAKE.get_post(pid))
            run(post.publish_existing_post(row, FakeBot(), only_channel_ids={2}))
        finally:
            post._post_to_telegram, post._post_to_bale = o1, o2

        after = FAKE.deliveries[(pid, 1)]
        self.assertEqual(after["attempts"], before["attempts"],
                         "a partial retry re-touched a channel that had already succeeded")
        self.assertEqual(after["status"], "completed")
