"""Regression tests: interactive workflows must be private-chat only.

Covers the three fixes:
  1. handlers/post.py  — only accept post content in private chat, not groups
  2. bot.py            — reject all non-private messages in route_message
  3. handlers/start.py — /start only works in private chat

Each handler is also exercised with the malformed updates that Telegram really
delivers for non-private chats (anonymous channel posts with no ``effective_user``,
edited messages with no ``message``), because the original guards were written
*after* the attribute access they were meant to protect and raised AttributeError
instead of returning.
"""

import asyncio
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _install_stubs():
    """Stub the database module so handlers import without a live MySQL server.

    Uses module ``__getattr__`` so every ``from database import ...`` resolves to
    a harmless async no-op, no matter which helper a handler asks for.
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

    def __getattr__(name):  # PEP 562 module-level __getattr__
        if name.startswith("__"):
            raise AttributeError(name)
        return _make(name)

    db.__getattr__ = __getattr__
    sys.modules["database"] = db


_install_stubs()

from utils import is_private_chat, private_actor  # noqa: E402

try:
    import handlers.post as post  # noqa: E402
    from handlers.post import (  # noqa: E402
        handle_any_message,
        handle_cancel_command,
        handle_text_post,
        handle_photo_post,
        handle_video_post,
        handle_document_post,
    )
    from handlers.start import start  # noqa: E402
except ImportError as exc:  # pragma: no cover - depends on install state
    raise unittest.SkipTest(
        f"python-telegram-bot is required for handler tests: {exc}"
    ) from exc

NON_PRIVATE = ("group", "supergroup", "channel")
USER_ID = 4242


def make_message(text="hello", photo=None, video=None, document=None):
    sent = []

    async def reply_text(*a, **k):
        sent.append(a[0] if a else k.get("text"))

    return types.SimpleNamespace(
        text=text, photo=photo, video=video, document=document,
        caption="", media_group_id=None, chat=types.SimpleNamespace(id=1),
        reply_text=reply_text, _sent=sent,
    )


def make_update(chat_type="private", user_id=USER_ID, message=-1):
    """Build a minimal Update stand-in.

    ``user_id=None``  -> no effective_user  (anonymous channel post)
    ``message=None``  -> no message         (edited/service update)
    """
    if message == -1:
        message = make_message()
    return types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(type=chat_type, id=1),
        effective_user=None if user_id is None else types.SimpleNamespace(id=user_id),
        message=message,
        callback_query=None,
    )


def run(coro):
    return asyncio.run(coro)


class HelperTests(unittest.TestCase):
    def test_only_private_is_accepted(self):
        self.assertTrue(is_private_chat(make_update("private")))
        for chat_type in NON_PRIVATE:
            with self.subTest(chat_type=chat_type):
                self.assertFalse(is_private_chat(make_update(chat_type)))

    def test_missing_chat_or_user_or_message_is_rejected(self):
        no_chat = make_update()
        no_chat.effective_chat = None
        self.assertFalse(is_private_chat(no_chat))
        self.assertIsNone(private_actor(no_chat))
        self.assertIsNone(private_actor(make_update("private", user_id=None)))
        self.assertIsNone(private_actor(make_update("private", message=None)))

    def test_private_actor_returns_user_in_private_chat(self):
        actor = private_actor(make_update("private"))
        self.assertIsNotNone(actor)
        self.assertEqual(actor.id, USER_ID)


class PostContentTests(unittest.TestCase):
    """Fix 1: handlers/post.py accepts post content only in private chat."""

    def setUp(self):
        post.user_states.clear()
        post.user_states[USER_ID] = {
            "state": "awaiting_post", "media": [], "caption": "",
            "created_at": time.monotonic(),
        }

    def tearDown(self):
        post.user_states.clear()

    def test_group_text_never_becomes_a_post(self):
        for chat_type in NON_PRIVATE:
            with self.subTest(chat_type=chat_type):
                update = make_update(chat_type)
                run(handle_any_message(update, None))
                # state untouched and nothing was sent back into the group
                self.assertEqual(post.user_states[USER_ID]["state"], "awaiting_post")
                self.assertEqual(update.message._sent, [])

    def test_private_text_is_accepted(self):
        update = make_update("private")
        run(handle_any_message(update, None))
        self.assertEqual(post.user_states[USER_ID]["state"], "awaiting_confirm")
        self.assertEqual(len(update.message._sent), 1)

    def test_anonymous_channel_post_does_not_crash(self):
        update = make_update("channel", user_id=None)
        run(handle_any_message(update, None))  # must not raise
        self.assertEqual(post.user_states[USER_ID]["state"], "awaiting_post")

    def test_update_without_message_does_not_crash(self):
        run(handle_any_message(make_update("private", message=None), None))
        self.assertEqual(post.user_states[USER_ID]["state"], "awaiting_post")

    def test_individual_content_handlers_reject_non_private(self):
        photo = [types.SimpleNamespace(file_id="p1")]
        video = types.SimpleNamespace(file_id="v1")
        document = types.SimpleNamespace(file_id="d1", file_name="a.pdf")
        cases = (
            (handle_text_post, make_message()),
            (handle_photo_post, make_message(text=None, photo=photo)),
            (handle_video_post, make_message(text=None, video=video)),
            (handle_document_post, make_message(text=None, document=document)),
        )
        for handler, message in cases:
            for chat_type in NON_PRIVATE:
                with self.subTest(handler=handler.__name__, chat_type=chat_type):
                    update = make_update(chat_type, message=message)
                    self.assertFalse(run(handler(update, None)))
                    self.assertEqual(post.user_states[USER_ID]["state"], "awaiting_post")

    def test_cancel_command_is_ignored_in_groups(self):
        for chat_type in NON_PRIVATE:
            with self.subTest(chat_type=chat_type):
                update = make_update(chat_type)
                run(handle_cancel_command(update, None))
                self.assertEqual(update.message._sent, [])
        # ...but still clears state in private chat
        run(handle_cancel_command(make_update("private"), None))
        self.assertNotIn(USER_ID, post.user_states)


class RouteMessageTests(unittest.TestCase):
    """Fix 2: bot.py route_message rejects every non-private message."""

    @staticmethod
    def build_router(calls):
        """Mirror of bot.route_message without importing bot.py (needs BOT_TOKEN)."""
        async def handle_restore_document(update, context):
            calls.append("restore")
            return False

        async def handle_add_user_input(update, context):
            calls.append("add_user")
            return False

        async def handle_edit_input(update, context):
            calls.append("edit")
            return False

        async def handle_channel_input(update, context):
            calls.append("channel")
            return False

        async def handle_any(update, context):
            calls.append("post")

        async def route_message(update, context):
            if private_actor(update) is None:
                return
            if await handle_restore_document(update, context):
                return
            if await handle_add_user_input(update, context):
                return
            if await handle_edit_input(update, context):
                return
            if await handle_channel_input(update, context):
                return
            await handle_any(update, context)

        return route_message

    def test_no_workflow_runs_for_non_private_chats(self):
        for chat_type in NON_PRIVATE:
            with self.subTest(chat_type=chat_type):
                calls = []
                run(self.build_router(calls)(make_update(chat_type), None))
                self.assertEqual(calls, [])

    def test_all_workflows_run_for_private_chat(self):
        calls = []
        run(self.build_router(calls)(make_update("private"), None))
        self.assertEqual(calls, ["restore", "add_user", "edit", "channel", "post"])

    def test_malformed_updates_are_dropped_without_crashing(self):
        for update in (
            make_update("channel", user_id=None),
            make_update("private", message=None),
            make_update("supergroup", user_id=None, message=None),
        ):
            calls = []
            run(self.build_router(calls)(update, None))
            self.assertEqual(calls, [])

    def test_registered_filter_is_private_only(self):
        """The MessageHandler must be registered with a PRIVATE chat-type filter."""
        bot_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")
        with open(bot_py, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("filters.ChatType.PRIVATE", source)


class StartCommandTests(unittest.TestCase):
    """Fix 3: /start only works in private chat."""

    def setUp(self):
        post.user_states.clear()

    def tearDown(self):
        post.user_states.clear()

    def test_start_is_silent_in_non_private_chats(self):
        for chat_type in NON_PRIVATE:
            with self.subTest(chat_type=chat_type):
                update = make_update(chat_type)
                run(start(update, None))
                self.assertEqual(update.message._sent, [])

    def test_start_does_not_wipe_state_from_a_group(self):
        """A group /start must not cancel the user's in-progress private workflow."""
        post.user_states[USER_ID] = {
            "state": "awaiting_post", "media": [], "caption": "",
            "created_at": time.monotonic(),
        }
        run(start(make_update("supergroup"), None))
        self.assertIn(USER_ID, post.user_states)

    def test_start_replies_in_private_chat(self):
        update = make_update("private")
        run(start(update, None))
        self.assertEqual(len(update.message._sent), 1)

    def test_anonymous_channel_start_does_not_crash(self):
        run(start(make_update("channel", user_id=None), None))


if __name__ == "__main__":
    unittest.main()
