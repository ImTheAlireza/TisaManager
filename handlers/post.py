import asyncio
import io
import logging
import json
import time
from datetime import datetime, timedelta

from telegram import Update, InputMediaPhoto, InputMediaVideo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from config import (
    RETRY_INTERVAL_MINUTES, SCHEDULE_GRACE_SECONDS, WORKFLOW_TTL_SECONDS,
    BALE_MAX_CONCURRENT,
)
from database import (
    get_active_channels, is_writer_or_above, is_sudo, is_owner, save_post,
    update_post_message_ids, update_post_delivery, create_schedule, get_due_schedules,
    update_schedule, has_permission, update_post_status, get_setting,
    claim_schedule, reclaim_stale_schedules, expire_stale_schedules,
    get_active_schedule_for_post, record_delivery, get_due_retries,
    claim_delivery_retry, reclaim_stale_retries, get_post_deliveries,
    save_workflow_session, delete_workflow_session, load_workflow_sessions,
    purge_workflow_sessions, get_user_role, get_post,
)
import jalali
from keyboards import (
    confirm_keyboard, main_menu_keyboard, channel_selection_keyboard,
    schedule_date_keyboard, schedule_hour_keyboard, schedule_minute_keyboard,
    schedule_calendar_keyboard,
)
from utils import (
    html_text, is_private_chat, private_actor,
    now_local, local_to_utc_naive, format_local, format_local_date,
    format_clock, date_example, parse_user_datetime, LOCAL_TZ,
)

logger = logging.getLogger(__name__)

# Per-user state tracking
user_states: dict[int, dict] = {}
STATE_TTL_SECONDS = WORKFLOW_TTL_SECONDS

# Publishes currently in flight. A restart waits for these to drain so a
# half-sent post is never abandoned mid-broadcast.
_inflight_publishes: set[int] = set()
_inflight_lock = asyncio.Lock()

# Keys that cannot be JSON-serialised into workflow_sessions (Telegram objects).
_UNPERSISTABLE_STATE_KEYS = {"message"}


class SchedulePastError(ValueError):
    """The requested schedule time is not in the future.

    A dedicated type so a genuine DB/JSON ValueError is never misreported to
    the user as "you picked a past time".
    """


def _serialisable_state(state: dict) -> dict:
    out = {}
    for key, value in state.items():
        if key in _UNPERSISTABLE_STATE_KEYS:
            continue
        if isinstance(value, (datetime,)):
            out[key] = value.isoformat()
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


async def persist_state(user_id: int, state: dict):
    """Mirror a workflow state to the DB so a restart cannot destroy it."""
    try:
        await save_workflow_session(user_id, "post", _serialisable_state(state))
    except Exception:
        # Persistence is best-effort: never break the user's flow over it.
        logger.exception("Could not persist workflow state for %s", user_id)


async def forget_state(user_id: int):
    user_states.pop(user_id, None)
    try:
        await delete_workflow_session(user_id)
    except Exception:
        logger.exception("Could not clear workflow state for %s", user_id)


async def restore_workflow_states(context: ContextTypes.DEFAULT_TYPE = None):
    """Reload unexpired workflow states after a restart.

    Without this, a restart silently drops whatever every user was composing;
    their next message lands with no state and is ignored.
    """
    try:
        rows = await load_workflow_sessions(WORKFLOW_TTL_SECONDS)
    except Exception:
        logger.exception("Could not restore workflow sessions")
        return 0
    restored = 0
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except Exception:
            continue
        # created_at is monotonic and meaningless across processes; rebase it so
        # the restored state expires WORKFLOW_TTL_SECONDS from now at the latest.
        payload["created_at"] = time.monotonic()
        payload["restored"] = True
        if payload.get("schedule_date"):
            try:
                payload["schedule_date"] = datetime.fromisoformat(payload["schedule_date"]).date()
            except (TypeError, ValueError):
                payload.pop("schedule_date", None)
        # A state that was awaiting a media group cannot be resumed: the album
        # debounce job died with the process. Send it back to plain composing.
        if payload.get("state") == "awaiting_media_group":
            payload["state"] = "awaiting_post"
        user_states[row["user_id"]] = payload
        restored += 1
    if restored:
        logger.info("Restored %d interactive workflow(s) after restart", restored)
    try:
        await purge_workflow_sessions(WORKFLOW_TTL_SECONDS)
    except Exception:
        logger.exception("Could not purge expired workflow sessions")
    return restored


def _active_state(user_id: int):
    state = user_states.get(user_id)
    if state and time.monotonic() - state.get("created_at", 0) > STATE_TTL_SECONDS:
        user_states.pop(user_id, None)
        # Drop the durable copy too, otherwise a later restart could restore a
        # workflow the user has already timed out of.
        try:
            asyncio.get_running_loop().create_task(delete_workflow_session(user_id))
        except RuntimeError:  # no loop (unit tests) — the periodic purge covers it
            pass
        return None
    return state


async def has_inflight_publishes() -> bool:
    async with _inflight_lock:
        return bool(_inflight_publishes)


async def inflight_count() -> int:
    async with _inflight_lock:
        return len(_inflight_publishes)


async def wait_for_inflight(timeout: float) -> bool:
    """Block until every in-flight publish finishes, or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not await has_inflight_publishes():
            return True
        await asyncio.sleep(0.5)
    return not await has_inflight_publishes()


async def _process_media_group_callback(context: ContextTypes.DEFAULT_TYPE):
    """Job callback that fires after media group debounce."""
    user_id = context.job.data
    state = _active_state(user_id)
    if not state or state.get("state") != "awaiting_media_group":
        return
    media = state.get("media", [])
    caption = state.get("caption", "")
    if not media:
        return

    state["state"] = "awaiting_confirm"
    state["type"] = "media_group"
    await persist_state(user_id, state)

    lines = ["📝 <b>پیش‌نمایش پست:</b>\n"]
    if caption:
        lines.append(f"کپشن: {html_text(caption)}")
    lines.append(f"\n📦 تعداد رسانه‌ها: {len(media)}")
    lines.append("\nبه همه کانال‌ها ارسال شود؟")

    msg = state["message"]
    await context.bot.send_message(
        chat_id=msg.chat.id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )


def _schedule_media_group(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Cancel existing job and schedule a new one in 1 second."""
    current_jobs = context.job_queue.get_jobs_by_name(f"media_group_{user_id}")
    for job in current_jobs:
        job.schedule_removal()
    context.job_queue.run_once(
        _process_media_group_callback,
        when=1.0,
        data=user_id,
        name=f"media_group_{user_id}",
    )


async def handle_new_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Never arm a posting workflow from a shared chat. The global gate in bot.py
    # already stops this, but arming state here would leave the user's next
    # private message silently captured as post content, so refuse locally too.
    if not is_private_chat(update):
        return

    if not await is_writer_or_above(query.from_user.id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    user_id = query.from_user.id
    state = {"state": "awaiting_post", "media": [], "caption": "", "created_at": time.monotonic()}
    user_states[user_id] = state
    await persist_state(user_id, state)
    await query.edit_message_text(
        "📝 پست خود را ارسال کنید.\n\n"
        "می‌توانید ارسال کنید:\n"
        "• پیام متنی\n"
        "• عکس با کپشن\n"
        "• ویدیو با کپشن\n"
        "• فایل با کپشن\n"
        "• گروه رسانه (چند عکس/ویدیو)"
    )


async def _handle_media_item(update, context, media_type, file_id):
    """Common handler for photo/video in a media group or single."""
    actor = private_actor(update)
    if actor is None:
        return False
    user_id = actor.id
    state = _active_state(user_id)
    if not state or state.get("state") not in ("awaiting_post", "awaiting_media_group"):
        return False

    msg = update.message
    caption = msg.caption or ""

    if msg.media_group_id:
        if state.get("state") != "awaiting_media_group":
            state["state"] = "awaiting_media_group"
            state["media"] = []
            state["caption"] = ""
            state["message"] = msg

        state["media"].append({"type": media_type, "file_id": file_id})
        if caption:
            state["caption"] = caption

        await persist_state(user_id, state)
        _schedule_media_group(user_id, context)
        return True

    state["type"] = media_type
    state["file_id"] = file_id
    state["caption"] = caption
    state["state"] = "awaiting_confirm"
    state["message"] = msg
    await persist_state(user_id, state)

    preview_lines = ["📝 <b>پیش‌نمایش پست:</b>\n"]
    if caption:
        preview_lines.append(f"کپشن: {html_text(caption)}")
    preview_lines.append("\nبه همه کانال‌ها ارسال شود؟")

    await msg.reply_text(
        "\n".join(preview_lines),
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )
    return True


async def handle_photo_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if private_actor(update) is None or not update.message.photo:
        return False
    photo = update.message.photo[-1]
    return await _handle_media_item(update, context, "photo", photo.file_id)


async def handle_video_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if private_actor(update) is None or not update.message.video:
        return False
    video = update.message.video
    return await _handle_media_item(update, context, "video", video.file_id)


async def handle_text_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = private_actor(update)
    if actor is None:
        return False
    user_id = actor.id
    state = _active_state(user_id)
    if not state or state.get("state") != "awaiting_post":
        return False

    text = update.message.text
    state["type"] = "text"
    state["text"] = text
    state["state"] = "awaiting_confirm"
    state["message"] = update.message
    await persist_state(user_id, state)

    await update.message.reply_text(
        f"📝 <b>پیش‌نمایش پست:</b>\n\n{html_text(text)}\n\nبه همه کانال‌ها ارسال شود؟",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )
    return True


async def handle_document_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = private_actor(update)
    if actor is None:
        return False
    user_id = actor.id
    state = _active_state(user_id)
    if not state or state.get("state") not in ("awaiting_post", "awaiting_media_group"):
        return False

    msg = update.message
    doc = msg.document
    caption = msg.caption or ""

    state["type"] = "document"
    state["file_id"] = doc.file_id
    state["caption"] = caption
    state["state"] = "awaiting_confirm"
    state["message"] = msg
    await persist_state(user_id, state)

    preview_lines = ["📝 <b>پیش‌نمایش پست:</b>\n"]
    if caption:
        preview_lines.append(f"کپشن: {html_text(caption)}")
    preview_lines.append(f"\n📎 فایل: {html_text(doc.file_name)}")
    preview_lines.append("\nبه همه کانال‌ها ارسال شود؟")

    await msg.reply_text(
        "\n".join(preview_lines),
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )
    return True


async def _post_to_telegram(channels, state, bot):
    """Returns (sent, failed, message_ids, errors_by_channel_id)."""
    sent = 0
    failed = 0
    message_ids = []
    errors: dict[int, str] = {}
    post_type = state.get("type")
    for ch in channels:
        try:
            result = None
            if post_type == "text":
                result = await bot.send_message(chat_id=ch["chat_id"], text=state["text"])
            elif post_type == "photo":
                result = await bot.send_photo(chat_id=ch["chat_id"], photo=state["file_id"], caption=state.get("caption"))
            elif post_type == "video":
                result = await bot.send_video(chat_id=ch["chat_id"], video=state["file_id"], caption=state.get("caption"))
            elif post_type == "document":
                result = await bot.send_document(chat_id=ch["chat_id"], document=state["file_id"], caption=state.get("caption"))
            elif post_type == "media_group":
                caption = state.get("caption")
                media_items = []
                for i, m in enumerate(state["media"]):
                    cap = caption if i == 0 and caption else None
                    if m["type"] == "photo":
                        media_items.append(InputMediaPhoto(m["file_id"], caption=cap, parse_mode=ParseMode.HTML if cap else None))
                    elif m["type"] == "video":
                        media_items.append(InputMediaVideo(m["file_id"], caption=cap, parse_mode=ParseMode.HTML if cap else None))
                result = await bot.send_media_group(chat_id=ch["chat_id"], media=media_items)
            if result:
                if isinstance(result, (list, tuple)):
                    for msg in result:
                        message_ids.append({"chat_id": ch["chat_id"], "message_id": msg.message_id, "platform": "telegram"})
                else:
                    message_ids.append({"chat_id": ch["chat_id"], "message_id": result.message_id, "platform": "telegram"})
            sent += 1
        except Exception as e:
            logger.error("Failed to post to %s (%s): %s", ch["name"], ch["chat_id"], e)
            errors[ch["id"]] = str(e)
            failed += 1
    return sent, failed, message_ids, errors


async def _download_telegram_file(bot, file_id) -> bytes:
    """Download one Telegram file fully into memory."""
    file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    return buf.getvalue()


async def _prepare_bale_payloads(state, bot) -> dict:
    """Build the shared payload for this post, downloading each file once.

    Previously every channel re-downloaded every file from Telegram, so an
    N-file album to M channels meant N*M downloads. Now each file is fetched
    a single time and reused for every destination.
    """
    post_type = state.get("type")
    if post_type == "text":
        return {"kind": "text"}
    if post_type == "media_group":
        items = []
        for m in state.get("media") or []:
            items.append((m["type"], await _download_telegram_file(bot, m["file_id"])))
        return {"kind": "media_group", "items": items}
    data = await _download_telegram_file(bot, state["file_id"])
    return {"kind": post_type, "bytes": data}


async def _send_bale_channel(client, ch, state, prepared):
    """Send one Bale channel from the shared payload.

    Returns (ok, message_ids, error).
    """
    kind = prepared.get("kind")
    caption = state.get("caption")
    if kind == "text":
        result = await client.send_message(ch["chat_id"], state["text"])
    elif kind == "photo":
        result = await client.send_photo(ch["chat_id"], prepared["bytes"], caption=caption)
    elif kind == "video":
        result = await client.send_video(ch["chat_id"], prepared["bytes"], caption=caption)
    elif kind == "document":
        result = await client.send_document(ch["chat_id"], prepared["bytes"], caption=caption)
    elif kind == "media_group":
        result = await client.send_media_group(ch["chat_id"], prepared["items"], caption=caption)
    else:
        return False, [], f"unsupported post type: {kind}"

    if result and result.get("ok"):
        ids = []
        msg = result["result"]
        if isinstance(msg, list):
            for m in msg:
                ids.append({"chat_id": ch["chat_id"], "message_id": m["message_id"], "platform": "bale"})
        else:
            ids.append({"chat_id": ch["chat_id"], "message_id": msg["message_id"], "platform": "bale"})
        return True, ids, None
    if result and not result.get("ok"):
        # A non-ok Bale response is a failure, not a success.
        return False, [], result.get("description", "Bale API error")
    return False, [], "empty Bale response"


async def _post_to_bale(channels, state, bot, attempt_no: int = 1):
    """Returns (sent, failed, message_ids, errors_by_channel_id).

    ``attempt_no`` selects which Bale bot sends: attempts alternate between
    the primary bot and the backup bot (if configured), so a rate-limited or
    blocked bot is swapped for a fresh one on the next try.

    Optimised for speed: media is downloaded from Telegram once per post and
    the channels are uploaded in parallel (BALE_MAX_CONCURRENT), instead of
    the old serial download-per-channel loop.
    """
    import bale_client
    sent = 0
    failed = 0
    message_ids = []
    errors: dict[int, str] = {}
    client = bale_client.client_for_attempt(attempt_no)
    if client is None:
        # No Bale token configured at all; report every channel as failed.
        for ch in channels:
            errors[ch["id"]] = "Bale token not configured"
        return 0, len(channels), [], errors

    try:
        prepared = await _prepare_bale_payloads(state, bot)
    except Exception as e:
        logger.error("Could not prepare Bale media (%s): %s", state.get("type"), e)
        for ch in channels:
            errors[ch["id"]] = str(e)
        return 0, len(channels), [], errors

    semaphore = asyncio.Semaphore(max(1, BALE_MAX_CONCURRENT))

    async def send_one(ch):
        async with semaphore:
            return await _send_bale_channel(client, ch, state, prepared)

    results = await asyncio.gather(*(send_one(ch) for ch in channels), return_exceptions=True)

    for ch, result in zip(channels, results):
        if isinstance(result, Exception):
            logger.error("Failed to post to Bale %s (%s) via %s: %s",
                         ch["name"], ch["chat_id"], client.name, result)
            errors[ch["id"]] = str(result)
            failed += 1
            continue
        ok, ids, error = result
        if ok:
            sent += 1
            message_ids.extend(ids)
        else:
            logger.error("Failed to post to Bale %s (%s) via %s: %s",
                         ch["name"], ch["chat_id"], client.name, error)
            errors[ch["id"]] = error
            failed += 1
    return sent, failed, message_ids, errors


def _next_retry_at():
    """Naive-UTC time of the next automatic retry, or None when disabled.

    Failed channels are retried on a fixed RETRY_INTERVAL_MINUTES cadence
    until they succeed or the retries are stopped from the post history —
    there is no attempt cap.
    """
    if RETRY_INTERVAL_MINUTES <= 0:
        return None
    return datetime.utcnow() + timedelta(minutes=RETRY_INTERVAL_MINUTES)


async def _record_channel_results(post_id: int, channels, failures: dict):
    """Persist one row per channel and arm retries for the failures."""
    for ch in channels:
        error = failures.get(ch["id"])
        if error is None:
            await record_delivery(post_id, ch["id"], ch.get("platform", "telegram"), "completed")
        else:
            await record_delivery(
                post_id, ch["id"], ch.get("platform", "telegram"), "failed",
                error=str(error)[:1000], next_retry_at=_next_retry_at(),
            )


async def _resolve_targets(post: dict, only_channel_ids: set = None):
    """Channels this post should go to.

    An explicitly empty target list means "no channels" — it must never be
    reinterpreted as "every channel", which would broadcast a post to
    destinations the author never chose (including ones added later).
    """
    raw = post.get("target_channels_json")
    tg = await get_active_channels("telegram")
    bale = await get_active_channels("bale")
    if raw is not None:
        try:
            selected = set(json.loads(raw or "[]"))
        except (TypeError, ValueError):
            selected = None
        if selected is not None:
            tg = [c for c in tg if c["id"] in selected]
            bale = [c for c in bale if c["id"] in selected]
    if only_channel_ids is not None:
        tg = [c for c in tg if c["id"] in only_channel_ids]
        bale = [c for c in bale if c["id"] in only_channel_ids]
    return tg, bale


async def split_live_targets(post: dict, channel_ids: set) -> tuple[set, set]:
    """Split ``channel_ids`` into (live, dead) against the post's targets.

    A channel is dead when it no longer exists or is inactive. Retrying a
    dead channel can never succeed, so callers must finalise those rows
    instead of re-queueing them forever.
    """
    tg, bale = await _resolve_targets(post)
    alive = {c["id"] for c in tg + bale}
    live = {i for i in channel_ids if i in alive}
    return live, channel_ids - live


async def publish_existing_post(post: dict, bot, only_channel_ids: set = None,
                                attempt_no: int = 1) -> tuple[int, int]:
    """Publish a stored post, used by scheduled jobs and retry actions.

    Registers itself as in-flight so a restart can wait for it to finish.
    ``attempt_no`` is this delivery's attempt number and only decides which
    Bale bot sends (attempts alternate bots); scheduling is unaffected.
    """
    post_id = post["id"]
    async with _inflight_lock:
        _inflight_publishes.add(post_id)
    try:
        tg, bale = await _resolve_targets(post, only_channel_ids)
        state = {"type": post["post_type"], "text": post.get("text"), "file_id": post.get("file_id"),
                 "caption": post.get("caption"), "media": json.loads(post.get("media_json") or "[]")}
        tg_sent, tg_failed, tg_ids, tg_errors = await _post_to_telegram(tg, state, bot)
        bale_sent, bale_failed, bale_ids, bale_errors = await _post_to_bale(bale, state, bot, attempt_no)

        # Merge with anything already delivered so a partial retry does not
        # erase the message ids of the channels that succeeded earlier.
        existing = []
        if only_channel_ids is not None:
            try:
                existing = json.loads(post.get("tg_message_ids") or "[]")
            except (TypeError, ValueError):
                existing = []
            existing = [m for m in existing if m.get("chat_id") not in
                        {c["chat_id"] for c in tg + bale}]
        await update_post_message_ids(post_id, json.dumps(existing + tg_ids + bale_ids), None)

        failures = {**tg_errors, **bale_errors}
        await _record_channel_results(post_id, tg + bale, failures)

        total = len(tg) + len(bale)
        sent = tg_sent + bale_sent
        if only_channel_ids is not None:
            # Partial retry: derive the post-level status from every channel.
            status = await _aggregate_status(post_id)
        else:
            status = "completed" if total and sent == total else ("partial" if sent else "failed")
        await update_post_delivery(
            post_id, status,
            json.dumps({"telegram_failed": tg_failed, "bale_failed": bale_failed}),
        )
        return sent, tg_failed + bale_failed
    finally:
        async with _inflight_lock:
            _inflight_publishes.discard(post_id)


async def _aggregate_status(post_id: int) -> str:
    rows = await get_post_deliveries(post_id)
    if not rows:
        return "failed"
    done = sum(1 for r in rows if r["status"] == "completed")
    if done == len(rows):
        return "completed"
    return "partial" if done else "failed"


async def refresh_delivery_status(post_id: int) -> str:
    """Recompute and persist the post-level status from its per-channel rows.

    Used after retries are cancelled so the post settles on its final,
    incomplete status (partial/failed) instead of staying "pending"-ish.
    """
    status = await _aggregate_status(post_id)
    await update_post_delivery(post_id, status, None)
    return status


async def _notify(bot, user_id: int, text: str):
    """Best-effort DM to the post author; never let it break a job."""
    if not user_id:
        return
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        logger.warning("Could not notify user %s", user_id, exc_info=True)


async def process_scheduled_posts(context: ContextTypes.DEFAULT_TYPE):
    """Publish everything that is due.

    Each row is claimed with a conditional UPDATE before any message is sent,
    so a crash or an overlapping tick can never publish the same post twice.
    """
    # Hand back rows whose worker died mid-publish, and warn about the ones
    # that have now been abandoned for good.
    try:
        for row in await reclaim_stale_schedules():
            await _notify(
                context.bot, row["user_id"],
                f"⚠️ زمان‌بندی پست #{row['post_id']} پس از چند بار قطع شدن متوقف شد. "
                "لطفاً به‌صورت دستی بررسی کنید.",
            )
    except Exception:
        logger.exception("Stale schedule recovery failed")

    # Retire schedules that are too old to be worth publishing.
    try:
        for row in await expire_stale_schedules(SCHEDULE_GRACE_SECONDS):
            await _notify(
                context.bot, row["user_id"],
                f"⏰ زمان‌بندی پست #{row['post_id']} برای "
                f"{format_local(row['run_at'])} از دست رفت (ربات در دسترس نبود) و لغو شد.\n"
                "در صورت نیاز دوباره زمان‌بندی کنید.",
            )
    except Exception:
        logger.exception("Schedule expiry sweep failed")

    for schedule in await get_due_schedules():
        if not await claim_schedule(schedule["id"]):
            # Someone else got it first.
            continue
        try:
            post = await get_post(schedule["post_id"])
            if not post:
                await update_schedule(schedule["id"], "failed", "post not found")
                continue

            # Re-check authorisation at fire time: the author may have been
            # demoted or removed since scheduling.
            if not await is_writer_or_above(schedule["user_id"]):
                await update_schedule(schedule["id"], "cancelled", "author no longer authorised")
                await update_post_delivery(post["id"], "failed", "author no longer authorised")
                logger.warning("Schedule %s skipped: user %s not authorised",
                               schedule["id"], schedule["user_id"])
                continue

            # Approval is enforced here too, otherwise scheduling is a way to
            # publish without review.
            approval_required = (await get_setting("approval_required", "0")) == "1"
            if approval_required and not await has_permission(schedule["user_id"], "approve"):
                await update_post_status(post["id"], "pending_approval")
                await update_schedule(schedule["id"], "completed", "sent for approval")
                await _notify(
                    context.bot, schedule["user_id"],
                    f"🔐 پست زمان‌بندی‌شده #{post['id']} برای تأیید مالک ارسال شد.",
                )
                from config import SUDO_USER_ID
                await _notify(
                    context.bot, SUDO_USER_ID,
                    f"🔐 پست زمان‌بندی‌شده #{post['id']} در انتظار تأیید شماست.",
                )
                continue

            await update_post_status(post["id"], "pending")
            sent, failed = await publish_existing_post(post, context.bot)
            await update_schedule(schedule["id"], "completed" if not failed else "failed",
                                  None if not failed else f"{failed} channel(s) failed")

            if failed:
                retry_note = ""
                if RETRY_INTERVAL_MINUTES:
                    retry_note = f"\n🔁 تلاش مجدد خودکار تا {RETRY_INTERVAL_MINUTES} دقیقه دیگر انجام می‌شود."
                await _notify(
                    context.bot, schedule["user_id"],
                    f"⚠️ پست زمان‌بندی‌شده #{post['id']} ناقص ارسال شد.\n"
                    f"✅ موفق: {sent} | ❌ ناموفق: {failed}{retry_note}",
                )
            else:
                await _notify(
                    context.bot, schedule["user_id"],
                    f"✅ پست زمان‌بندی‌شده #{post['id']} با موفقیت به {sent} مقصد ارسال شد.",
                )
        except Exception as exc:
            logger.exception("Scheduled post %s failed", schedule["id"])
            await update_schedule(schedule["id"], "failed", str(exc)[:1000])
            await _notify(
                context.bot, schedule["user_id"],
                f"❌ ارسال پست زمان‌بندی‌شده #{schedule['post_id']} با خطا مواجه شد:\n"
                f"<code>{html_text(str(exc)[:300])}</code>",
            )


async def process_delivery_retries(context: ContextTypes.DEFAULT_TYPE):
    """Re-send only the channels that failed, every RETRY_INTERVAL_MINUTES.

    Retries repeat on the fixed interval until every channel succeeds or they
    are stopped from the post history — there is no attempt cap.
    """
    try:
        await reclaim_stale_retries()
    except Exception:
        logger.exception("Stale retry recovery failed")

    due = await get_due_retries()
    if not due:
        return

    # Group by post so a post failing on three channels produces one publish
    # and one notification, not three. Channels are also split by attempt
    # parity: Bale attempts alternate bots, so channels whose next attempt
    # falls on different bots must not share a publish.
    by_key: dict[tuple, list[dict]] = {}
    for row in due:
        if await claim_delivery_retry(row["id"]):
            by_key.setdefault((row["post_id"], row["attempts"] % 2), []).append(row)

    for (post_id, parity), rows in by_key.items():
        # Rows carry how many attempts already happened; the next send is
        # attempt attempts+1. Even attempts (2, 4, ...) go to the backup bot,
        # odd ones (1, 3, ...) to the primary.
        attempt_no = 2 if parity else 1
        # Updated by the try-block; the except must only re-arm rows that are
        # still live — dead channels were finalised and must stay that way.
        live_ids = {r["channel_id"] for r in rows}
        try:
            post = await get_post(post_id)
            if not post:
                # The post was deleted; drop the orphaned retry rows.
                for r in rows:
                    await record_delivery(post_id, r["channel_id"], r["platform"],
                                          "cancelled", "post deleted", None)
                continue

            # Channels that were removed/deactivated since the failure can
            # never be delivered; finalise them instead of retrying forever.
            live_ids, dead_ids = await split_live_targets(post, live_ids)
            for r in rows:
                if r["channel_id"] in dead_ids:
                    await record_delivery(post_id, r["channel_id"], r["platform"],
                                          "cancelled", "channel no longer available", None)
            if not live_ids:
                continue

            sent, failed = await publish_existing_post(
                post, context.bot, only_channel_ids=live_ids, attempt_no=attempt_no,
            )
            if failed:
                await _notify(
                    context.bot, post["user_id"],
                    f"⚠️ تلاش مجدد پست #{post_id} برای {failed} مقصد ناموفق بود. "
                    f"تلاش بعدی {RETRY_INTERVAL_MINUTES} دقیقه دیگر انجام می‌شود.",
                )
            else:
                final_status = await _aggregate_status(post_id)
                if final_status == "completed":
                    await _notify(
                        context.bot, post["user_id"],
                        f"✅ تلاش مجدد موفق بود: پست #{post_id} به {sent} مقصد باقی‌مانده ارسال شد "
                        "و کامل شد.",
                    )
                else:
                    await _notify(
                        context.bot, post["user_id"],
                        f"✅ تلاش مجدد موفق بود: {sent} مقصد باقی‌ماندهٔ پست #{post_id} ارسال شد.",
                    )
        except Exception as exc:
            logger.exception("Delivery retry for post %s failed", post_id)
            retry_at = _next_retry_at()
            for r in rows:
                if r["channel_id"] not in live_ids:
                    continue
                await record_delivery(post_id, r["channel_id"], r["platform"], "failed",
                                      str(exc)[:1000], retry_at)


async def handle_confirm_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    state = _active_state(user_id)
    if not state or state.get("state") != "awaiting_confirm":
        await query.edit_message_text("❌ پستی در انتظار نیست. برای شروع /start را بزنید.")
        return

    tg_channels = await get_active_channels("telegram")
    bale_channels = await get_active_channels("bale")
    selected_ids = state.get("selected_channel_ids")
    if selected_ids:
        selected_ids = set(selected_ids)
        tg_channels = [c for c in tg_channels if c["id"] in selected_ids]
        bale_channels = [c for c in bale_channels if c["id"] in selected_ids]

    logger.info("Found %d Telegram channels, %d Bale channels", len(tg_channels), len(bale_channels))

    if not tg_channels and not bale_channels:
        await query.edit_message_text("❌ کانالی تنظیم نشده است. از تنظیمات کانال اضافه کنید.")
        await forget_state(user_id)
        return

    await query.edit_message_text("⏳ در حال ارسال به کانال‌ها...")

    post_type = state.get("type")

    # Approval is an owner-controlled global setting and defaults to off.
    approval_required = (await get_setting("approval_required", "0")) == "1"

    # Save to history first
    media_json = json.dumps(state.get("media")) if post_type == "media_group" else None
    post_id = await save_post(
        user_id, post_type,
        text=state.get("text"),
        file_id=state.get("file_id"),
        caption=state.get("caption"),
        media_json=media_json,
        target_channels_json=json.dumps([c["id"] for c in tg_channels + bale_channels]),
        delivery_status="pending_approval" if approval_required and not await has_permission(user_id, "approve") else "pending",
    )

    if approval_required and not await has_permission(user_id, "approve"):
        await forget_state(user_id)
        await query.edit_message_text(f"📝 پست #{post_id} برای تأیید مالک ارسال شد.", reply_markup=main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id)))
        return

    async with _inflight_lock:
        _inflight_publishes.add(post_id)
    try:
        tg_sent, tg_failed, tg_message_ids, tg_errors = await _post_to_telegram(tg_channels, state, context.bot)
        # First attempt: always the primary Bale bot.
        bale_sent, bale_failed, bale_message_ids, bale_errors = await _post_to_bale(
            bale_channels, state, context.bot, attempt_no=1,
        )

        # Update history with all message IDs
        all_message_ids = tg_message_ids + bale_message_ids
        await update_post_message_ids(post_id, json.dumps(all_message_ids), None)
        total = len(tg_channels) + len(bale_channels)
        total_sent = tg_sent + bale_sent
        delivery_status = "completed" if total_sent == total else ("partial" if total_sent else "failed")
        delivery_errors = {"telegram_failed": tg_failed, "bale_failed": bale_failed}
        await update_post_delivery(post_id, delivery_status, json.dumps(delivery_errors))
        # Per-channel rows arm the automatic retries.
        await _record_channel_results(
            post_id, tg_channels + bale_channels, {**tg_errors, **bale_errors},
        )
    finally:
        async with _inflight_lock:
            _inflight_publishes.discard(post_id)

    await forget_state(user_id)

    total_failed = tg_failed + bale_failed

    result = f"✅ ارسال شد به {total_sent}/{total} کانال."
    if tg_channels:
        result += f"\n📣 تلگرام: {tg_sent}/{len(tg_channels)}"
    if bale_channels:
        result += f"\n🔵 بله: {bale_sent}/{len(bale_channels)}"
    if total_failed:
        result += f"\n❌ ناموفق: {total_failed}"
        if RETRY_INTERVAL_MINUTES:
            result += f"\n🔁 تلاش مجدد خودکار تا {RETRY_INTERVAL_MINUTES} دقیقه دیگر."

    from database import is_sudo as _is_sudo, is_owner as _is_owner
    await context.bot.send_message(
        chat_id=query.message.chat.id,
        text=result,
        reply_markup=main_menu_keyboard(is_sudo=await _is_sudo(query.from_user.id), is_owner=await _is_owner(query.from_user.id)),
    )


async def _channel_picker(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    state = _active_state(user_id)
    if not state or state.get("state") != "awaiting_confirm":
        await query.answer("❌ پستی در انتظار نیست.", show_alert=True)
        return
    channels = await get_active_channels()
    selected = set(state.get("selected_channel_ids", [c["id"] for c in channels]))
    state["selected_channel_ids"] = list(selected)
    await query.edit_message_text(
        "🎯 کانال‌های مقصد را انتخاب کنید:",
        reply_markup=channel_selection_keyboard(channels, selected),
    )
    await query.answer()


async def handle_choose_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _channel_picker(update, context)


async def handle_toggle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    state = _active_state(query.from_user.id)
    if not state:
        await query.answer("❌ نشست منقضی شده است.", show_alert=True)
        return
    channel_id = int(query.data.removeprefix("toggle_channel_"))
    selected = set(state.get("selected_channel_ids", []))
    if channel_id in selected:
        selected.remove(channel_id)
    else:
        selected.add(channel_id)
    state["selected_channel_ids"] = list(selected)
    await persist_state(query.from_user.id, state)
    channels = await get_active_channels()
    await query.edit_message_reply_markup(reply_markup=channel_selection_keyboard(channels, selected))
    await query.answer()


async def handle_channels_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    state = _active_state(query.from_user.id)
    selected = state.get("selected_channel_ids", []) if state else []
    if not selected:
        await query.answer("حداقل یک کانال را انتخاب کنید.", show_alert=True)
        return
    await query.edit_message_text("✅ کانال‌ها انتخاب شدند. برای ادامه یکی از گزینه‌ها را انتخاب کنید.", reply_markup=confirm_keyboard())
    await query.answer()


async def handle_channels_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.edit_message_text("📝 پست آماده است. مقصدهای انتخاب‌شده حفظ شدند.", reply_markup=confirm_keyboard())
    await query.answer()


async def _save_current_post(user_id, state, delivery_status="draft"):
    media_json = json.dumps(state.get("media")) if state.get("type") == "media_group" else None
    return await save_post(
        user_id, state.get("type"), text=state.get("text"), file_id=state.get("file_id"),
        caption=state.get("caption"), media_json=media_json,
        target_channels_json=json.dumps(state.get("selected_channel_ids", [])),
        delivery_status=delivery_status,
    )


async def handle_save_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Answer first: the DB round-trip below can outlive the ~15s window in
    # which Telegram still accepts an answer for this callback.
    await query.answer()
    state = _active_state(query.from_user.id)
    if not state or state.get("state") != "awaiting_confirm":
        await query.answer("\u274c پستی در انتظار نیست.", show_alert=True)
        return
    post_id = await _save_current_post(query.from_user.id, state)
    await forget_state(query.from_user.id)
    await query.edit_message_text(
        f"\U0001f4be پیش‌نویس #{post_id} ذخیره شد.",
        reply_markup=main_menu_keyboard(is_sudo=await is_sudo(query.from_user.id), is_owner=await is_owner(query.from_user.id)),
    )


# --- Scheduling wizard -------------------------------------------------------
#
# Every step carries its own data in the callback payload and verifies the
# workflow phase before touching state. A stale button from an earlier post can
# therefore never hijack or crash the current one.

_SCHEDULE_PHASES = {
    "awaiting_schedule", "awaiting_schedule_date",
    "awaiting_schedule_hour", "awaiting_schedule_minute",
}

_STALE_BUTTON_MSG = "\u274c این دکمه مربوط به یک عملیات قدیمی است. دوباره از «پست جدید» شروع کنید."


async def _require_phase(query, phases: set):
    """Return the state only if it is really in one of ``phases``."""
    state = _active_state(query.from_user.id)
    if not state:
        await query.answer("\u274c نشست منقضی شده است.", show_alert=True)
        return None
    if state.get("state") not in phases:
        await query.answer(_STALE_BUTTON_MSG, show_alert=True)
        return None
    return state


async def handle_schedule_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def handle_legacy_schedule_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Absorb schedule buttons created by the previous release.

    At deploy time users still have old keyboards on screen whose payloads
    ("schedule_date_today", "schedule_hour_14", ...) no longer match any
    pattern. Without this they would spin forever.
    """
    query = update.callback_query
    await query.answer(
        "این دکمه مربوط به نسخه قبلی است. لطفاً دوباره «🕒 زمان‌بندی» را بزنید.",
        show_alert=True,
    )


async def handle_schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = _active_state(query.from_user.id)
    if not state or state.get("state") != "awaiting_confirm":
        await query.answer("\u274c پستی در انتظار نیست.", show_alert=True)
        return

    # Writers must not be able to use scheduling as an approval bypass.
    approval_required = (await get_setting("approval_required", "0")) == "1"
    if approval_required and not await has_permission(query.from_user.id, "approve"):
        await query.edit_message_text(
            "\U0001f510 تأیید پیش از انتشار فعال است؛ زمان‌بندی برای شما در دسترس نیست.\n"
            "پست را با «تأیید و ارسال» بفرستید تا برای تأیید مالک ارسال شود.",
            reply_markup=confirm_keyboard(),
        )
        return

    channels = await get_active_channels()
    if not channels:
        await query.edit_message_text("\u274c کانالی تنظیم نشده است. از تنظیمات کانال اضافه کنید.")
        await forget_state(query.from_user.id)
        return
    if not state.get("selected_channel_ids"):
        state["selected_channel_ids"] = [c["id"] for c in channels]

    state["state"] = "awaiting_schedule_date"
    await persist_state(query.from_user.id, state)
    await query.edit_message_text(
        _schedule_prompt("تاریخ انتشار را انتخاب کنید:"),
        parse_mode=ParseMode.HTML,
        reply_markup=schedule_date_keyboard(now_local().date()),
    )


def _schedule_prompt(headline: str) -> str:
    tzname = LOCAL_TZ.key if hasattr(LOCAL_TZ, "key") else str(LOCAL_TZ)
    today = now_local().date()
    return (
        f"\U0001f552 <b>{html_text(headline)}</b>\n"
        f"امروز: {html_text(format_local_date(today, long_form=True))}"
        f" — منطقه زمانی: {html_text(tzname)}\n\n"
        f"می‌توانید زمان را دستی هم بفرستید، نمونه:\n"
        f"<code>{html_text(date_example())}</code>"
    )


async def _finish_schedule(user_id: int, state: dict, local_time: datetime, update, context):
    """Persist the schedule. ``local_time`` is naive, in the display timezone."""
    now = now_local().replace(tzinfo=None)
    if local_time <= now:
        raise SchedulePastError("time is in the past")

    utc_time = local_to_utc_naive(local_time)
    # 'scheduled' keeps it out of the draft flow, so it can never be published
    # by hand and then a second time by the job.
    post_id = await _save_current_post(user_id, state, delivery_status="scheduled")
    schedule_id = await create_schedule(user_id, post_id, utc_time)
    await forget_state(user_id)

    tzname = LOCAL_TZ.key if hasattr(LOCAL_TZ, "key") else str(LOCAL_TZ)
    text = (
        f"\u2705 پست #{post_id} زمان‌بندی شد.\n"
        f"\U0001f552 {format_local_date(local_time.date(), long_form=True)}"
        f" ساعت {format_clock(local_time.hour, local_time.minute)} ({tzname})\n"
        f"\U0001f194 شناسه زمان‌بندی: #{schedule_id}\n\n"
        f"از «\U0001f552 پست‌های زمان‌بندی‌شده» می‌توانید آن را ببینید، تغییر دهید یا لغو کنید."
    )
    markup = main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id))
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def handle_schedule_back_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = await _require_phase(query, _SCHEDULE_PHASES)
    if not state:
        return
    state["state"] = "awaiting_schedule_date"
    await persist_state(query.from_user.id, state)
    await query.edit_message_text(
        _schedule_prompt("تاریخ انتشار را انتخاب کنید:"),
        parse_mode=ParseMode.HTML,
        reply_markup=schedule_date_keyboard(now_local().date()),
    )


async def handle_schedule_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a full Jalali month grid.

    Payload is either "schedule_cal" (current month) or
    "schedule_cal_<jy>_<jm>" when paging between months.
    """
    query = update.callback_query
    await query.answer()
    state = await _require_phase(query, _SCHEDULE_PHASES)
    if not state:
        return

    today = now_local().date()
    payload = query.data.removeprefix("schedule_cal")
    if payload.startswith("_"):
        try:
            jy_str, jm_str = payload.lstrip("_").split("_")
            jy, jm = int(jy_str), int(jm_str)
        except ValueError:
            await query.answer(_STALE_BUTTON_MSG, show_alert=True)
            return
        if not 1 <= jm <= 12:
            await query.answer(_STALE_BUTTON_MSG, show_alert=True)
            return
    else:
        jy, jm, _ = jalali.to_jalali(today)

    state["state"] = "awaiting_schedule_date"
    await persist_state(query.from_user.id, state)
    await query.edit_message_text(
        _schedule_prompt("روز انتشار را از تقویم انتخاب کنید:"),
        parse_mode=ParseMode.HTML,
        reply_markup=schedule_calendar_keyboard(jy, jm, today),
    )


async def handle_schedule_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = await _require_phase(query, _SCHEDULE_PHASES)
    if not state:
        return
    try:
        chosen = datetime.fromisoformat(query.data.removeprefix("schedule_date_")).date()
    except ValueError:
        await query.answer(_STALE_BUTTON_MSG, show_alert=True)
        return

    now = now_local()
    if chosen < now.date():
        await query.answer("\u274c این تاریخ گذشته است.", show_alert=True)
        return

    state["schedule_date"] = chosen
    state["state"] = "awaiting_schedule_hour"
    await persist_state(query.from_user.id, state)
    # Hide hours that have already passed when the user picks today.
    min_hour = now.hour if chosen == now.date() else 0
    await query.edit_message_text(
        _schedule_prompt(f"ساعت انتشار برای {format_local_date(chosen, long_form=True)}:"),
        parse_mode=ParseMode.HTML,
        reply_markup=schedule_hour_keyboard(chosen.isoformat(), min_hour=min_hour),
    )


async def handle_schedule_back_hour(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = await _require_phase(query, _SCHEDULE_PHASES)
    if not state:
        return
    try:
        chosen = datetime.fromisoformat(query.data.removeprefix("schedule_back_hour_")).date()
    except ValueError:
        await query.answer(_STALE_BUTTON_MSG, show_alert=True)
        return
    now = now_local()
    state["schedule_date"] = chosen
    state["state"] = "awaiting_schedule_hour"
    await persist_state(query.from_user.id, state)
    min_hour = now.hour if chosen == now.date() else 0
    await query.edit_message_text(
        _schedule_prompt(f"ساعت انتشار برای {format_local_date(chosen, long_form=True)}:"),
        parse_mode=ParseMode.HTML,
        reply_markup=schedule_hour_keyboard(chosen.isoformat(), min_hour=min_hour),
    )


async def handle_schedule_hour(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = await _require_phase(query, _SCHEDULE_PHASES)
    if not state:
        return
    payload = query.data.removeprefix("schedule_hour_")
    date_str, _, hour_str = payload.rpartition("_")
    try:
        chosen = datetime.fromisoformat(date_str).date()
        hour = int(hour_str)
    except ValueError:
        await query.answer(_STALE_BUTTON_MSG, show_alert=True)
        return
    if not 0 <= hour <= 23:
        await query.answer(_STALE_BUTTON_MSG, show_alert=True)
        return

    now = now_local()
    state["schedule_date"] = chosen
    state["schedule_hour"] = hour
    state["state"] = "awaiting_schedule_minute"
    await persist_state(query.from_user.id, state)

    # Only offer minutes still in the future for the current hour.
    if chosen == now.date() and hour == now.hour:
        allowed = [m for m in range(0, 60, 5) if m > now.minute]
    else:
        allowed = list(range(0, 60, 5))
    await query.edit_message_text(
        _schedule_prompt(
            f"دقیقه انتشار برای {format_local_date(chosen, long_form=True)}"
            f" ساعت {jalali.to_persian_digits(f'{hour:02d}')}:"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=schedule_minute_keyboard(chosen.isoformat(), hour, allowed_minutes=allowed),
    )


async def handle_schedule_minute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = await _require_phase(query, _SCHEDULE_PHASES)
    if not state:
        return
    payload = query.data.removeprefix("schedule_minute_")
    try:
        date_str, hour_str, minute_str = payload.rsplit("_", 2)
        chosen = datetime.fromisoformat(date_str).date()
        hour, minute = int(hour_str), int(minute_str)
        local_time = datetime.combine(chosen, datetime.min.time()).replace(hour=hour, minute=minute)
    except ValueError:
        await query.answer(_STALE_BUTTON_MSG, show_alert=True)
        return

    try:
        await _finish_schedule(query.from_user.id, state, local_time, update, context)
    except SchedulePastError:
        # Only a genuinely past time lands here; real failures propagate to the
        # global error handler instead of being mislabelled.
        await query.answer("\u274c این زمان گذشته است. زمان دیگری انتخاب کنید.", show_alert=True)


async def handle_schedule_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual 'YYYY-MM-DD HH:MM' entry during the scheduling wizard."""
    actor = private_actor(update)
    if actor is None:
        return False
    user_id = actor.id
    state = _active_state(user_id)
    if not state or state.get("state") not in _SCHEDULE_PHASES:
        return False
    if not update.message.text:
        return False

    try:
        local_time = parse_user_datetime(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(
            f"\u274c {html_text(exc)}\n\nنمونه درست:\n<code>{html_text(date_example())}</code>",
            parse_mode=ParseMode.HTML,
        )
        return True

    try:
        await _finish_schedule(user_id, state, local_time, update, context)
    except SchedulePastError:
        await update.message.reply_text("\u274c این زمان گذشته است. زمانی در آینده بفرستید.")
    return True


async def handle_cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    await forget_state(user_id)

    from database import is_sudo as _is_sudo2, is_owner as _is_owner2
    await query.edit_message_text(
        "❌ پست لغو شد.",
        reply_markup=main_menu_keyboard(is_sudo=await _is_sudo2(user_id), is_owner=await _is_owner2(user_id)),
    )


def cancel_all_workflows(user_id: int):
    """Drop every in-memory workflow for a user.

    Synchronous on purpose (callers are sync); the durable mirror is cleared by
    ``cancel_all_workflows_async`` where an event loop is available.
    """
    user_states.pop(user_id, None)
    # These modules keep their own short-lived workflow state.
    from handlers.history import _edit_states
    from handlers.settings import _settings_states
    from handlers.users import _add_user_states
    _edit_states.pop(user_id, None)
    _settings_states.pop(user_id, None)
    _add_user_states.pop(user_id, None)
    from handlers.schedules import cancel_reschedule
    cancel_reschedule(user_id)
    from backup import cancel_restore
    cancel_restore(user_id)


async def cancel_all_workflows_async(user_id: int):
    """Cancel workflows and forget the persisted copy too."""
    cancel_all_workflows(user_id)
    try:
        await delete_workflow_session(user_id)
    except Exception:
        logger.exception("Could not clear persisted workflow for %s", user_id)


async def handle_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel every interactive workflow belonging to the current user."""
    # /cancel is part of the private-chat workflow; ignore it in groups so the
    # bot stays silent in shared chats.
    actor = private_actor(update)
    if actor is None:
        return
    user_id = actor.id
    await cancel_all_workflows_async(user_id)
    await update.message.reply_text(
        "✅ عملیات لغو شد.",
        reply_markup=main_menu_keyboard(
            is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id)
        ),
    )


async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route incoming media/text to the correct handler based on user state."""
    # Only accept post content in private chat — group, supergroup and channel
    # messages must never trigger post submission (use the "new post" button).
    # This must happen before touching effective_user/message: anonymous channel
    # posts carry no user and edited updates carry no message.
    actor = private_actor(update)
    if actor is None:
        return
    user_id = actor.id
    state = _active_state(user_id)
    if not state:
        return

    if state.get("state") in _SCHEDULE_PHASES:
        await handle_schedule_input(update, context)
        return
    allowed_states = {"awaiting_post", "awaiting_media_group"}
    if state.get("state") not in allowed_states:
        return

    msg = update.message
    if msg.text:
        await handle_text_post(update, context)
    elif msg.photo:
        await handle_photo_post(update, context)
    elif msg.video:
        await handle_video_post(update, context)
    elif msg.document:
        await handle_document_post(update, context)
