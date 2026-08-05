import json
import logging
import time

from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from config import RETRY_INTERVAL_MINUTES
from database import (
    get_user_posts, get_all_posts, get_user_posts_paginated, get_all_posts_paginated,
    count_user_posts, count_all_posts, get_post, update_post_text, update_post_caption,
    delete_post, is_writer_or_above, is_owner, is_sudo, can_edit_post, can_delete_post,
    get_user_role, has_permission, update_post_status, save_post_version, save_post,
    get_active_schedule_for_post, cancel_schedule, get_post_deliveries,
    cancel_post_retries, record_delivery, claim_delivery_retry,
)
from keyboards import main_menu_keyboard, history_keyboard, post_detail_keyboard, confirm_keyboard
from utils import html_text, state_is_expired, format_local, format_local_date
from handlers.post import (
    publish_existing_post, user_states, refresh_delivery_status, split_live_targets,
)

logger = logging.getLogger(__name__)

_edit_states: dict[int, dict] = {}


async def _menu_kb(user_id):
    return main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id))


def _safe_parse_json(data):
    if not data:
        return []
    try:
        return json.loads(data)
    except Exception:
        return []


async def _try_edit_message(context, chat_id, message_id, platform, post, new_text):
    """Try to edit a single message. Returns True on success."""
    post_type = post.get("post_type")
    try:
        if platform == "bale":
            import bale_client
            if post_type == "text":
                result = await bale_client.edit_message_text(chat_id, message_id, new_text)
            else:
                result = await bale_client.edit_message_caption(chat_id, message_id, new_text)
            return result.get("ok", False)
        else:
            if post_type == "text":
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=new_text)
                return True

            # First try editing caption (works if caption already existed)
            try:
                await context.bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=new_text)
                return True
            except BadRequest:
                # If editing caption fails (e.g., adding caption to captionless media or media group),
                # fallback to edit_message_media
                if post_type == "photo":
                    media = InputMediaPhoto(media=post.get("file_id"), caption=new_text)
                    await context.bot.edit_message_media(chat_id=chat_id, message_id=message_id, media=media)
                    return True
                elif post_type == "video":
                    media = InputMediaVideo(media=post.get("file_id"), caption=new_text)
                    await context.bot.edit_message_media(chat_id=chat_id, message_id=message_id, media=media)
                    return True
                elif post_type == "document":
                    media = InputMediaDocument(media=post.get("file_id"), caption=new_text)
                    await context.bot.edit_message_media(chat_id=chat_id, message_id=message_id, media=media)
                    return True
                elif post_type == "media_group":
                    media_list = _safe_parse_json(post.get("media_json"))
                    if media_list:
                        first_media = media_list[0]
                        m_type = first_media.get("type")
                        m_file_id = first_media.get("file_id")
                        if m_type == "video":
                            media = InputMediaVideo(media=m_file_id, caption=new_text)
                        else:
                            media = InputMediaPhoto(media=m_file_id, caption=new_text)
                        await context.bot.edit_message_media(chat_id=chat_id, message_id=message_id, media=media)
                        return True
                raise
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return True
        logger.error("Edit failed [%s] chat=%s msg=%s: %s", platform, chat_id, message_id, e)
        return False
    except Exception as e:
        logger.error("Edit failed [%s] chat=%s msg=%s: %s", platform, chat_id, message_id, e)
        return False


async def _try_delete_message(context, chat_id, message_id, platform):
    """Try to delete a single message. Returns True on success."""
    try:
        if platform == "bale":
            import bale_client
            result = await bale_client.delete_message(chat_id, message_id)
            return result.get("ok", False)
        else:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True
    except BadRequest as e:
        if "message to delete not found" in str(e).lower() or "message to edit not found" in str(e).lower():
            return True
        logger.error("Delete failed [%s] chat=%s msg=%s: %s", platform, chat_id, message_id, e)
        return False
    except Exception as e:
        logger.error("Delete failed [%s] chat=%s msg=%s: %s", platform, chat_id, message_id, e)
        return False


async def handle_history_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()


async def handle_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_writer_or_above(query.from_user.id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    user_id = query.from_user.id
    role = await get_user_role(user_id)
    is_admin = role in ("sudo", "owner")

    page = 1
    if query.data.startswith("history_page_"):
        try:
            page = int(query.data.removeprefix("history_page_"))
        except ValueError:
            page = 1

    POSTS_PER_PAGE = 5
    offset = (page - 1) * POSTS_PER_PAGE

    if is_admin:
        total_posts = await count_all_posts()
        posts = await get_all_posts_paginated(limit=POSTS_PER_PAGE, offset=offset)
    else:
        total_posts = await count_user_posts(user_id)
        posts = await get_user_posts_paginated(user_id, limit=POSTS_PER_PAGE, offset=offset)

    total_pages = max(1, (total_posts + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * POSTS_PER_PAGE
        if is_admin:
            posts = await get_all_posts_paginated(limit=POSTS_PER_PAGE, offset=offset)
        else:
            posts = await get_user_posts_paginated(user_id, limit=POSTS_PER_PAGE, offset=offset)

    if not posts and total_posts == 0:
        try:
            await query.edit_message_text("📋 تاریخچه پست‌ها خالی است.", reply_markup=await _menu_kb(user_id))
        except BadRequest:
            pass
        return

    text = "📋 <b>تاریخچه پست‌ها:</b>\n\nروی یک پست کلیک کنید:"
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=history_keyboard(posts, page=page, total_pages=total_pages, is_admin=is_admin)
        )
    except BadRequest:
        pass


async def _post_detail_parts(post_id: int, viewer_id: int):
    """Build (text, markup) for the post detail view.

    Returns (None, None) when the post does not exist. The retry-management
    buttons are included only for viewers who may edit the post, and only
    while an automatic retry is actually queued.
    """
    post = await get_post(post_id)
    if not post:
        return None, None

    type_labels = {"text": "📝 متن", "photo": "🖼️ عکس", "video": "🎬 ویدیو", "document": "📎 فایل", "media_group": "📦 گروه رسانه"}
    label = type_labels.get(post["post_type"], post["post_type"])
    # created_at is stored UTC (the pool pins every session to +00:00), so it
    # must go through the same conversion as every other timestamp.
    date = format_local(post["created_at"]) if post["created_at"] else ""

    status_labels = {"pending": "⏳ در انتظار", "draft": "💾 پیش‌نویس", "scheduled": "🕒 زمان‌بندی‌شده", "pending_approval": "🔐 در انتظار تأیید", "completed": "✅ کامل", "partial": "⚠️ ناقص", "failed": "❌ ناموفق"}
    delivery_status = status_labels.get(post.get("delivery_status"), "نامشخص")
    lines = [f"<b>{label}</b>", f"📅 {date}", f"📤 وضعیت ارسال: {delivery_status}"]

    # Surface the schedule that owns this post, so a "scheduled" status is
    # never a dead end the user cannot act on.
    schedule = await get_active_schedule_for_post(post_id)
    if schedule:
        lines.append(f"🕒 زمان انتشار: {format_local(schedule['run_at'])} (#{schedule['id']})")

    # Show which channels failed, and whether a retry is queued.
    deliveries = await get_post_deliveries(post_id)
    failed_rows = [d for d in deliveries if d["status"] in ("failed", "retrying")]
    has_active_retries = any(
        d.get("next_retry_at") or d["status"] == "retrying" for d in failed_rows
    )
    if failed_rows:
        retry_state = "🔁 تلاش مجدد خودکار فعال است." if has_active_retries else "⏸️ تلاش بعدی زمان‌بندی نشده است."
        lines.append(f"❌ ناموفق در {len(failed_rows)} مقصد ({retry_state})")
        for d in failed_rows[:5]:
            name = d.get("channel_name") or f"#{d['channel_id']}"
            when = f" — تلاش بعدی {format_local(d['next_retry_at'])}" if d.get("next_retry_at") else ""
            lines.append(f"  • {html_text(name)}{when}")
    lines.append("")
    if post.get("text"):
        lines.append(f"📝 متن:\n{html_text(post['text'])}")
    if post.get("caption"):
        lines.append(f"💬 کپشن:\n{html_text(post['caption'])}")
    if post["post_type"] == "media_group" and post.get("media_json"):
        media = _safe_parse_json(post["media_json"])
        if media:
            lines.append(f"\n📦 تعداد رسانه‌ها: {len(media)}")

    # Count sent messages
    msg_ids = _safe_parse_json(post.get("tg_message_ids"))
    if msg_ids:
        tg_count = sum(1 for m in msg_ids if m.get("platform") == "telegram")
        bale_count = sum(1 for m in msg_ids if m.get("platform") == "bale")
        parts = []
        if tg_count:
            parts.append(f"📣 تلگرام: {tg_count}")
        if bale_count:
            parts.append(f"🔵 بله: {bale_count}")
        lines.append(f"\n{' | '.join(parts)}")

    text = "\n".join(lines)
    markup = post_detail_keyboard(
        post_id, post.get("delivery_status", "completed"),
        schedule_id=schedule["id"] if schedule else None,
        # Writers manage retries of their own posts; sudo/owner manage all.
        # (Writers only ever see their own posts in the history list anyway,
        # but this check also covers stale/shared callback data.)
        can_manage_retries=await can_edit_post(viewer_id, post_id),
        # Hidden once the post is fully sent or the retries were stopped.
        has_active_retries=has_active_retries,
    )
    return text, markup


async def handle_post_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_writer_or_above(query.from_user.id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    try:
        post_id = int(query.data.removeprefix("post_"))
    except (IndexError, ValueError):
        return

    text, markup = await _post_detail_parts(post_id, query.from_user.id)
    if text is None:
        try:
            await query.edit_message_text("❌ پست یافت نشد.", reply_markup=await _menu_kb(query.from_user.id))
        except BadRequest:
            await query.message.reply_text("❌ پست یافت نشد.", reply_markup=await _menu_kb(query.from_user.id))
        return

    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest:
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_writer_or_above(query.from_user.id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    try:
        post_id = int(query.data.removeprefix("edit_"))
    except (IndexError, ValueError):
        return

    if not await can_edit_post(query.from_user.id, post_id):
        await query.edit_message_text("❌ شما اجازه ویرایش این پست را ندارید.")
        return

    post = await get_post(post_id)
    if post and post.get("delivery_status") == "draft":
        await query.edit_message_text("❌ پیش‌نویس با این گزینه قابل ویرایش نیست. ابتدا آن را منتشر کنید.")
        return
    if post and post.get("delivery_status") == "scheduled":
        await query.edit_message_text(
            "❌ پست زمان‌بندی‌شده قابل ویرایش نیست. ابتدا زمان‌بندی را لغو کنید."
        )
        return
    if not post:
        try:
            await query.edit_message_text("❌ پست یافت نشد.", reply_markup=await _menu_kb(query.from_user.id))
        except BadRequest:
            await query.message.reply_text("❌ پست یافت نشد.", reply_markup=await _menu_kb(query.from_user.id))
        return

    hint = "متن جدید را ارسال کنید:" if post["post_type"] == "text" else "کپشن جدید را ارسال کنید:"
    try:
        await query.edit_message_text(f"✏️ <b>ویرایش پست</b>\n\n{hint}", parse_mode=ParseMode.HTML)
    except BadRequest:
        await query.message.reply_text(f"✏️ <b>ویرایش پست</b>\n\n{hint}", parse_mode=ParseMode.HTML)

    _edit_states[query.from_user.id] = {"post_id": post_id, "post": post, "created_at": time.monotonic()}


async def handle_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = _edit_states.get(user_id)
    if state and state_is_expired(state):
        _edit_states.pop(user_id, None)
        state = None
    if not state:
        return False
    if not update.message or not update.message.text:
        return False

    new_text = update.message.text.strip()
    post_id = state["post_id"]
    post = state["post"]
    _edit_states.pop(user_id, None)

    # Preserve the previous content before applying the edit.
    await save_post_version(post_id, user_id, post.get("text"), post.get("caption"))

    # Update database
    if post["post_type"] == "text":
        await update_post_text(post_id, new_text)
    else:
        await update_post_caption(post_id, new_text)

    # Edit messages on all platforms
    msg_ids = _safe_parse_json(post.get("tg_message_ids"))
    edited = 0
    is_media_group = post["post_type"] == "media_group"
    seen_channels = set()

    for i, item in enumerate(msg_ids):
        chat_id = item.get("chat_id")
        message_id = item.get("message_id")
        platform = item.get("platform", "telegram")
        if not chat_id or not message_id:
            continue

        # For media groups, only edit first message per channel
        channel_key = (chat_id, platform)
        if is_media_group and channel_key in seen_channels:
            continue
        seen_channels.add(channel_key)

        ok = await _try_edit_message(context, chat_id, message_id, platform, post, new_text)
        if ok:
            edited += 1

    result = "✅ پست به‌روزرسانی شد."
    if edited:
        result += f"\n📨 {edited} پیام ویرایش شد."

    await update.message.reply_text(result, reply_markup=await _menu_kb(update.effective_user.id))
    return True


async def handle_duplicate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        post_id = int(query.data.removeprefix("duplicate_"))
    except ValueError:
        return
    post = await get_post(post_id)
    if post and post.get("delivery_status") == "draft":
        await query.edit_message_text("❌ پیش‌نویس قابل کپی نیست. ابتدا آن را منتشر کنید.")
        return
    if post and post.get("delivery_status") == "scheduled":
        await query.edit_message_text("❌ پست زمان‌بندی‌شده قابل کپی نیست.")
        return
    if not post or not await can_edit_post(query.from_user.id, post_id):
        await query.edit_message_text("❌ اجازه کپی این پست را ندارید.")
        return
    state = {"state": "awaiting_confirm", "type": post["post_type"], "text": post.get("text"),
             "file_id": post.get("file_id"), "caption": post.get("caption") or "", "media": _safe_parse_json(post.get("media_json")),
             "selected_channel_ids": _safe_parse_json(post.get("target_channels_json")), "created_at": time.monotonic()}
    user_states[query.from_user.id] = state
    # Mirror to the DB so a restart does not lose the duplicated draft.
    from handlers.post import persist_state
    await persist_state(query.from_user.id, state)
    await query.edit_message_text("📄 کپی پست آماده است. مقصدها و عملیات را انتخاب کنید.", reply_markup=confirm_keyboard())


async def handle_publish_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    post_id = int(query.data.removeprefix("publish_draft_"))
    post = await get_post(post_id)
    if not post or post.get("delivery_status") != "draft":
        await query.edit_message_text("❌ فقط پیش‌نویس‌ها قابل انتشار هستند.")
        return
    if not await can_edit_post(query.from_user.id, post_id):
        await query.edit_message_text("❌ شما اجازه انتشار این پیش‌نویس را ندارید.")
        return
    # Defence in depth: a post owned by an open schedule must never be
    # published from here, or it goes out now *and* again when the job fires.
    active = await get_active_schedule_for_post(post_id)
    if active:
        await query.edit_message_text(
            f"❌ این پست برای {format_local(active['run_at'])} زمان‌بندی شده است.\n"
            "برای انتشار فوری از «🕒 پست‌های زمان‌بندی‌شده» اقدام کنید.",
        )
        return
    await update_post_status(post_id, "pending")
    await query.edit_message_text("⏳ در حال انتشار پیش‌نویس...")
    sent, failed = await publish_existing_post(post, context.bot)
    await query.message.reply_text(f"✅ انتشار انجام شد: {sent} موفق، {failed} ناموفق.", reply_markup=await _menu_kb(query.from_user.id))


async def handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await has_permission(query.from_user.id, "approve"):
        await query.answer("❌ فقط مالک می‌تواند پست را تأیید کند.", show_alert=True)
        return
    try:
        post_id = int(query.data.removeprefix("approve_"))
    except ValueError:
        return
    post = await get_post(post_id)
    if not post or post.get("delivery_status") != "pending_approval":
        await query.edit_message_text("❌ این پست در انتظار تأیید نیست.")
        return
    await query.edit_message_text("⏳ پست تأیید شد، در حال انتشار...")
    await update_post_status(post_id, "pending")
    sent, failed = await publish_existing_post(post, context.bot)
    await query.message.reply_text(f"✅ انتشار انجام شد: {sent} موفق، {failed} ناموفق.", reply_markup=await _menu_kb(query.from_user.id))


async def handle_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        post_id = int(query.data.removeprefix("retry_"))
    except ValueError:
        return
    if not await can_edit_post(query.from_user.id, post_id):
        await query.edit_message_text("❌ اجازه ارسال مجدد این پست را ندارید.")
        return
    post = await get_post(post_id)
    if not post:
        await query.edit_message_text("❌ پست یافت نشد.")
        return
    if post.get("delivery_status") == "draft":
        await query.edit_message_text("❌ پیش‌نویس را ابتدا با گزینه «انتشار پیش‌نویس» منتشر کنید.")
        return
    if post.get("delivery_status") == "scheduled" or await get_active_schedule_for_post(post_id):
        await query.edit_message_text(
            "❌ این پست زمان‌بندی شده است. تا زمان انتشار، ارسال مجدد ممکن نیست."
        )
        return
    # A republish is a new history row, so it can be edited/deleted independently.
    new_id = await save_post(
        query.from_user.id, post["post_type"], text=post.get("text"), file_id=post.get("file_id"),
        caption=post.get("caption"), media_json=post.get("media_json"),
        target_channels_json=post.get("target_channels_json"), delivery_status="pending",
    )
    new_post = dict(post)
    new_post["id"] = new_id
    await query.edit_message_text("⏳ در حال ارسال مجدد...")
    sent, failed = await publish_existing_post(new_post, context.bot)
    await query.message.reply_text(f"🔁 ارسال مجدد انجام شد و در تاریخچه با شناسه #{new_id} ذخیره شد: {sent} موفق، {failed} ناموفق.", reply_markup=await _menu_kb(query.from_user.id))


async def _parse_action_post_id(query, prefix: str):
    try:
        return int(query.data.removeprefix(prefix))
    except ValueError:
        return None


async def handle_retry_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Immediately re-send a post's failed channels.

    Available to sudo/owner for every post and to writers for their own
    posts. Unlike «🔁 ارسال مجدد», this does not create a new history row —
    it completes the same post. On success the post is marked complete; on
    failure the next automatic attempt is armed RETRY_INTERVAL_MINUTES from
    now.
    """
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    post_id = await _parse_action_post_id(query, "retry_now_")
    if post_id is None:
        return
    if not await can_edit_post(user_id, post_id):
        await query.answer("❌ شما اجازه مدیریت تلاش‌های مجدد این پست را ندارید.", show_alert=True)
        return
    post = await get_post(post_id)
    if not post:
        await query.edit_message_text("❌ پست یافت نشد.")
        return
    if post.get("delivery_status") in ("draft", "scheduled", "pending_approval"):
        await query.answer("❌ این پست هنوز منتشر نشده است که مقصد ناموفق داشته باشد.", show_alert=True)
        return
    deliveries = await get_post_deliveries(post_id)
    if any(d["status"] == "retrying" for d in deliveries):
        await query.answer("⏳ یک تلاش خودکار همین حالا در حال ارسال است؛ چند لحظه دیگر دوباره امتحان کنید.", show_alert=True)
        return
    failed_rows = [d for d in deliveries if d["status"] == "failed"]
    if not failed_rows:
        await query.answer("✅ همه مقصدهای این پست با موفقیت ارسال شده‌اند.", show_alert=True)
        return

    # Claim the rows exactly like the automatic job does, so the job cannot
    # pick one up mid-flight and double-send a channel. A row the job already
    # grabbed simply fails to claim and is left to the job.
    claimed = []
    for d in failed_rows:
        if await claim_delivery_retry(d["id"]):
            claimed.append(d)
    if not claimed:
        await query.answer("⏳ یک تلاش خودکار همین حالا در حال ارسال است؛ چند لحظه دیگر دوباره امتحان کنید.", show_alert=True)
        return
    failed_ids = {d["channel_id"] for d in claimed}

    # Channels removed since the failure can never be delivered; finalise
    # them instead of reporting a misleading "complete".
    live_ids, dead_ids = await split_live_targets(post, failed_ids)
    for d in deliveries:
        if d["channel_id"] in dead_ids and d["status"] == "failed":
            await record_delivery(post_id, d["channel_id"], d["platform"],
                                  "cancelled", "channel no longer available", None)
    if not live_ids:
        # Settle the post on its final incomplete status now that the dead
        # channels are finalised.
        await refresh_delivery_status(post_id)
        await query.edit_message_text(
            "⚠️ مقصدهای ناموفق این پست دیگر در دسترس نیستند (کانال حذف یا غیرفعال شده است).",
            reply_markup=await _menu_kb(user_id),
        )
        return

    await query.edit_message_text("⏳ در حال ارسال به مقصدهای ناموفق...")
    sent, failed = await publish_existing_post(post, context.bot, only_channel_ids=live_ids)
    final_status = await refresh_delivery_status(post_id)
    if failed == 0 and final_status == "completed":
        result = f"✅ پست #{post_id} کامل شد: {sent} مقصد باقی‌مانده با موفقیت ارسال شد."
    elif failed == 0:
        result = (
            f"✅ {sent} مقصد باقی‌ماندهٔ پست #{post_id} ارسال شد؛ "
            "برخی مقصدها دیگر در دسترس نیستند و پست ناقص ماند."
        )
    elif RETRY_INTERVAL_MINUTES:
        result = (
            f"❌ ارسال به {failed} مقصد دوباره ناموفق بود.\n"
            f"🔁 تلاش بعدی {RETRY_INTERVAL_MINUTES} دقیقه دیگر انجام می‌شود."
        )
    else:
        result = (
            f"❌ ارسال به {failed} مقصد دوباره ناموفق بود.\n"
            "🔁 تلاش مجدد خودکار غیرفعال است."
        )
    await query.message.reply_text(result, reply_markup=await _menu_kb(user_id))


async def handle_cancel_retries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel every pending automatic retry for a post.

    Available to sudo/owner for every post and to writers for their own
    posts. All future attempts are dropped and the post's current delivery
    result is accepted as final (incomplete): partial if some channels were
    sent, otherwise failed.
    """
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    post_id = await _parse_action_post_id(query, "cancel_retries_")
    if post_id is None:
        return
    if not await can_edit_post(user_id, post_id):
        await query.answer("❌ شما اجازه مدیریت تلاش‌های مجدد این پست را ندارید.", show_alert=True)
        return
    post = await get_post(post_id)
    if not post:
        await query.edit_message_text("❌ پست یافت نشد.")
        return
    if post.get("delivery_status") in ("draft", "scheduled", "pending_approval"):
        await query.answer("❌ این پست هنوز منتشر نشده است که تلاش مجددی داشته باشد.", show_alert=True)
        return

    deliveries = await get_post_deliveries(post_id)
    armed = sum(1 for d in deliveries if d["status"] == "failed" and d.get("next_retry_at"))
    inflight = sum(1 for d in deliveries if d["status"] == "retrying")
    if not armed and not inflight:
        # Nothing queued: do NOT touch the post status, or a stale button
        # could flip a healthy post to "failed".
        await query.answer("ℹ️ تلاش مجددی برای این پست در صف نیست.", show_alert=True)
        return

    cleared = await cancel_post_retries(post_id)
    status = await refresh_delivery_status(post_id)
    status_labels = {"completed": "✅ کامل", "partial": "⚠️ ناقص", "failed": "❌ ناموفق"}
    final_label = status_labels.get(status, status)
    note = f"🚫 تلاش‌های خودکار برای پست #{post_id} متوقف شد و دیگر تلاشی انجام نمی‌شود."
    if cleared:
        note += f"\n({cleared} تلاش در صف لغو شد.)"
    note += f"\n📤 وضعیت نهایی ارسال: {final_label}"

    text, markup = await _post_detail_parts(post_id, user_id)
    if text is None:
        await query.edit_message_text(note, reply_markup=await _menu_kb(user_id))
        return
    try:
        await query.edit_message_text(f"{note}\n\n{text}", parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest:
        await query.message.reply_text(f"{note}\n\n{text}", parse_mode=ParseMode.HTML, reply_markup=markup)


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_writer_or_above(query.from_user.id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    try:
        post_id = int(query.data.removeprefix("delete_"))
    except (IndexError, ValueError):
        return

    if not await can_delete_post(query.from_user.id, post_id):
        await query.edit_message_text("❌ شما اجازه حذف این پست را ندارید.")
        return

    post = await get_post(post_id)
    if not post:
        try:
            await query.edit_message_text("❌ پست یافت نشد.", reply_markup=await _menu_kb(query.from_user.id))
        except BadRequest:
            await query.message.reply_text("❌ پست یافت نشد.", reply_markup=await _menu_kb(query.from_user.id))
        return

    # Stop any pending schedule first, otherwise the job later fires on a post
    # that no longer exists. A publish already in flight must not be deleted
    # underneath the worker.
    active = await get_active_schedule_for_post(post_id)
    if active:
        if active["status"] == "processing":
            await query.answer(
                "❌ این پست هم‌اکنون در حال ارسال است. بعداً دوباره تلاش کنید.", show_alert=True,
            )
            return
        await cancel_schedule(active["id"])

    # Delete messages from all platforms
    msg_ids = _safe_parse_json(post.get("tg_message_ids"))
    deleted = 0
    for item in msg_ids:
        chat_id = item.get("chat_id")
        message_id = item.get("message_id")
        platform = item.get("platform", "telegram")
        if chat_id and message_id:
            ok = await _try_delete_message(context, chat_id, message_id, platform)
            if ok:
                deleted += 1

    await delete_post(post_id)

    result = "✅ پست حذف شد."
    if deleted:
        result += f"\n🗑️ {deleted} پیام حذف شد."

    try:
        await query.edit_message_text(result, reply_markup=await _menu_kb(query.from_user.id))
    except BadRequest:
        await query.message.reply_text(result, reply_markup=await _menu_kb(query.from_user.id))
