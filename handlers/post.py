import logging
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InputMediaPhoto, InputMediaVideo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database import (
    get_active_channels, is_writer_or_above, is_sudo, is_owner, save_post,
    update_post_message_ids, update_post_delivery, create_schedule, get_due_schedules,
    update_schedule, has_permission, update_post_status, get_setting,
)
from keyboards import confirm_keyboard, main_menu_keyboard, channel_selection_keyboard, schedule_date_keyboard, schedule_hour_keyboard, schedule_minute_keyboard
from utils import html_text, private_actor

logger = logging.getLogger(__name__)

# Per-user state tracking
user_states: dict[int, dict] = {}
STATE_TTL_SECONDS = 30 * 60


def _active_state(user_id: int):
    state = user_states.get(user_id)
    if state and time.monotonic() - state.get("created_at", 0) > STATE_TTL_SECONDS:
        user_states.pop(user_id, None)
        return None
    return state


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

    if not await is_writer_or_above(query.from_user.id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    user_id = query.from_user.id
    user_states[user_id] = {"state": "awaiting_post", "media": [], "caption": "", "created_at": time.monotonic()}
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

        _schedule_media_group(user_id, context)
        return True

    state["type"] = media_type
    state["file_id"] = file_id
    state["caption"] = caption
    state["state"] = "awaiting_confirm"
    state["message"] = msg

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
    sent = 0
    failed = 0
    message_ids = []
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
            failed += 1
    return sent, failed, message_ids


async def _post_to_bale(channels, state, bot):
    import bale_client
    import tempfile
    import os
    sent = 0
    failed = 0
    message_ids = []
    post_type = state.get("type")
    tmp_dir = tempfile.mkdtemp(prefix="bale_post_")
    try:
        for ch in channels:
            try:
                result = None
                if post_type == "text":
                    result = await bale_client.send_message(ch["chat_id"], state["text"])
                elif post_type == "photo":
                    path = os.path.join(tmp_dir, f"photo_{ch['id']}.jpg")
                    file = await bot.get_file(state["file_id"])
                    await file.download_to_drive(path)
                    with open(path, "rb") as f:
                        result = await bale_client.send_photo(ch["chat_id"], f.read(), caption=state.get("caption"))
                elif post_type == "video":
                    path = os.path.join(tmp_dir, f"video_{ch['id']}.mp4")
                    file = await bot.get_file(state["file_id"])
                    await file.download_to_drive(path)
                    with open(path, "rb") as f:
                        result = await bale_client.send_video(ch["chat_id"], f.read(), caption=state.get("caption"))
                elif post_type == "document":
                    path = os.path.join(tmp_dir, f"doc_{ch['id']}.bin")
                    file = await bot.get_file(state["file_id"])
                    await file.download_to_drive(path)
                    with open(path, "rb") as f:
                        result = await bale_client.send_document(ch["chat_id"], f.read(), caption=state.get("caption"))
                elif post_type == "media_group":
                    caption = state.get("caption")
                    media_files = []
                    for i, m in enumerate(state["media"]):
                        ext = "jpg" if m["type"] == "photo" else "mp4"
                        path = os.path.join(tmp_dir, f"media_{i}.{ext}")
                        file = await bot.get_file(m["file_id"])
                        await file.download_to_drive(path)
                        with open(path, "rb") as f:
                            media_files.append((m["type"], f.read()))
                    result = await bale_client.send_media_group(ch["chat_id"], media_files, caption=caption)
                if result and result.get("ok"):
                    msg = result["result"]
                    if isinstance(msg, list):
                        for m in msg:
                            message_ids.append({"chat_id": ch["chat_id"], "message_id": m["message_id"], "platform": "bale"})
                    else:
                        message_ids.append({"chat_id": ch["chat_id"], "message_id": msg["message_id"], "platform": "bale"})
                sent += 1
            except Exception as e:
                logger.error("Failed to post to Bale %s (%s): %s", ch["name"], ch["chat_id"], e)
                failed += 1
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return sent, failed, message_ids


async def publish_existing_post(post: dict, bot) -> tuple[int, int]:
    """Publish a stored post, used by scheduled jobs and retry actions."""
    selected = set(json.loads(post.get("target_channels_json") or "[]"))
    tg = await get_active_channels("telegram")
    bale = await get_active_channels("bale")
    if selected:
        tg = [c for c in tg if c["id"] in selected]
        bale = [c for c in bale if c["id"] in selected]
    state = {"type": post["post_type"], "text": post.get("text"), "file_id": post.get("file_id"),
             "caption": post.get("caption"), "media": json.loads(post.get("media_json") or "[]")}
    tg_sent, tg_failed, tg_ids = await _post_to_telegram(tg, state, bot)
    bale_sent, bale_failed, bale_ids = await _post_to_bale(bale, state, bot)
    await update_post_message_ids(post["id"], json.dumps(tg_ids + bale_ids), None)
    total = len(tg) + len(bale)
    sent = tg_sent + bale_sent
    status = "completed" if sent == total and total else ("partial" if sent else "failed")
    await update_post_delivery(post["id"], status, json.dumps({"telegram_failed": tg_failed, "bale_failed": bale_failed}))
    return sent, tg_failed + bale_failed


async def process_scheduled_posts(context: ContextTypes.DEFAULT_TYPE):
    from database import get_post
    for schedule in await get_due_schedules():
        try:
            post = await get_post(schedule["post_id"])
            if not post:
                await update_schedule(schedule["id"], "failed", "post not found")
                continue
            await publish_existing_post(post, context.bot)
            await update_schedule(schedule["id"], "completed")
        except Exception as exc:
            logger.exception("Scheduled post %s failed", schedule["id"])
            await update_schedule(schedule["id"], "failed", str(exc)[:1000])


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
        user_states.pop(user_id, None)
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
        user_states.pop(user_id, None)
        await query.edit_message_text(f"📝 پست #{post_id} برای تأیید مالک ارسال شد.", reply_markup=main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id)))
        return

    tg_sent, tg_failed, tg_message_ids = await _post_to_telegram(tg_channels, state, context.bot)
    bale_sent, bale_failed, bale_message_ids = await _post_to_bale(bale_channels, state, context.bot)

    # Update history with all message IDs
    all_message_ids = tg_message_ids + bale_message_ids
    await update_post_message_ids(post_id, json.dumps(all_message_ids), None)
    total = len(tg_channels) + len(bale_channels)
    total_sent = tg_sent + bale_sent
    delivery_status = "completed" if total_sent == total else ("partial" if total_sent else "failed")
    delivery_errors = {"telegram_failed": tg_failed, "bale_failed": bale_failed}
    await update_post_delivery(post_id, delivery_status, json.dumps(delivery_errors))

    user_states.pop(user_id, None)

    total_sent = tg_sent + bale_sent
    total_failed = tg_failed + bale_failed
    total = len(tg_channels) + len(bale_channels)

    result = f"✅ ارسال شد به {total_sent}/{total} کانال."
    if tg_channels:
        result += f"\n📣 تلگرام: {tg_sent}/{len(tg_channels)}"
    if bale_channels:
        result += f"\n🔵 بله: {bale_sent}/{len(bale_channels)}"
    if total_failed:
        result += f"\n❌ ناموفق: {total_failed}"

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
    state = _active_state(query.from_user.id)
    if not state or state.get("state") != "awaiting_confirm":
        await query.answer("❌ پستی در انتظار نیست.", show_alert=True)
        return
    post_id = await _save_current_post(query.from_user.id, state)
    user_states.pop(query.from_user.id, None)
    await query.edit_message_text(f"💾 پیش‌نویس #{post_id} ذخیره شد.", reply_markup=main_menu_keyboard(is_sudo=await is_sudo(query.from_user.id), is_owner=await is_owner(query.from_user.id)))
    await query.answer()


async def handle_schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    state = _active_state(query.from_user.id)
    if not state or state.get("state") != "awaiting_confirm":
        await query.answer("❌ پستی در انتظار نیست.", show_alert=True)
        return
    if not state.get("selected_channel_ids"):
        channels = await get_active_channels()
        state["selected_channel_ids"] = [c["id"] for c in channels]
    state["state"] = "awaiting_schedule_date"
    await query.edit_message_text("🕒 تاریخ انتشار را انتخاب کنید (Asia/Tehran):", reply_markup=schedule_date_keyboard())
    await query.answer()


async def _finish_schedule(user_id: int, state: dict, tehran_time: datetime, update, context):
    tz = ZoneInfo("Asia/Tehran")
    now = datetime.now(tz)
    if tehran_time <= now.replace(tzinfo=None):
        raise ValueError("past")
    utc_time = tehran_time.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    post_id = await _save_current_post(user_id, state)
    await create_schedule(user_id, post_id, utc_time)
    user_states.pop(user_id, None)
    text = f"✅ پست #{post_id} برای {tehran_time:%Y-%m-%d %H:%M} به وقت تهران زمان‌بندی شد."
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id)))
    else:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id)))


async def handle_schedule_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    state = _active_state(query.from_user.id)
    if not state:
        await query.answer("❌ نشست منقضی شده است.", show_alert=True)
        return
    offset = 0 if query.data.endswith("today") else 1
    state["schedule_date"] = (datetime.now(ZoneInfo("Asia/Tehran")) + timedelta(days=offset)).date()
    state["state"] = "awaiting_schedule_hour"
    await query.edit_message_text("ساعت انتشار را انتخاب کنید:", reply_markup=schedule_hour_keyboard())
    await query.answer()


async def handle_schedule_hour(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    state = _active_state(query.from_user.id)
    if not state:
        await query.answer("❌ نشست منقضی شده است.", show_alert=True)
        return
    hour = int(query.data.removeprefix("schedule_hour_"))
    state["schedule_hour"] = hour
    state["state"] = "awaiting_schedule_minute"
    await query.edit_message_text("دقیقه انتشار را انتخاب کنید:", reply_markup=schedule_minute_keyboard(hour))
    await query.answer()


async def handle_schedule_minute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    state = _active_state(query.from_user.id)
    if not state:
        await query.answer("❌ نشست منقضی شده است.", show_alert=True)
        return
    _, _, hour, minute = query.data.split("_")
    tehran_time = datetime.combine(state["schedule_date"], datetime.min.time()).replace(hour=int(hour), minute=int(minute))
    try:
        await _finish_schedule(query.from_user.id, state, tehran_time, update, context)
    except ValueError:
        await query.answer("❌ این زمان گذشته است. دوباره زمان دیگری انتخاب کنید.", show_alert=True)
        return
    await query.answer()


async def handle_schedule_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = private_actor(update)
    if actor is None:
        return False
    user_id = actor.id
    state = _active_state(user_id)
    if not state or state.get("state") not in {"awaiting_schedule", "awaiting_schedule_date", "awaiting_schedule_hour", "awaiting_schedule_minute"}:
        return False
    if not update.message.text:
        return False
    try:
        tehran_time = datetime.strptime(update.message.text.strip(), "%Y-%m-%d %H:%M")
        await _finish_schedule(user_id, state, tehran_time, update, context)
    except ValueError:
        await update.message.reply_text("❌ زمان گذشته یا فرمت نامعتبر است. نمونه: 2026-08-01 14:30 (تهران)")
    return True


async def handle_cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    user_states.pop(user_id, None)

    from database import is_sudo as _is_sudo2, is_owner as _is_owner2
    await query.edit_message_text(
        "❌ پست لغو شد.",
        reply_markup=main_menu_keyboard(is_sudo=await _is_sudo2(user_id), is_owner=await _is_owner2(user_id)),
    )


def cancel_all_workflows(user_id: int):
    user_states.pop(user_id, None)
    # These modules keep their own short-lived workflow state.
    from handlers.history import _edit_states
    from handlers.settings import _settings_states
    from handlers.users import _add_user_states
    _edit_states.pop(user_id, None)
    _settings_states.pop(user_id, None)
    _add_user_states.pop(user_id, None)
    from backup import cancel_restore
    cancel_restore(user_id)


async def handle_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel every interactive workflow belonging to the current user."""
    # /cancel is part of the private-chat workflow; ignore it in groups so the
    # bot stays silent in shared chats.
    actor = private_actor(update)
    if actor is None:
        return
    user_id = actor.id
    cancel_all_workflows(user_id)
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

    if state.get("state") in {"awaiting_schedule", "awaiting_schedule_date", "awaiting_schedule_hour", "awaiting_schedule_minute"}:
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
