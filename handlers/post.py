import logging
import json

from telegram import Update, InputMediaPhoto, InputMediaVideo
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database import get_active_channels, is_writer_or_above, is_sudo, is_owner, save_post, update_post_message_ids
from keyboards import confirm_keyboard, main_menu_keyboard

logger = logging.getLogger(__name__)

# Per-user state tracking
user_states: dict[int, dict] = {}


async def _process_media_group_callback(context: ContextTypes.DEFAULT_TYPE):
    """Job callback that fires after media group debounce."""
    user_id = context.job.data
    state = user_states.get(user_id)
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
        lines.append(f"کپشن: {caption}")
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
    user_states[user_id] = {"state": "awaiting_post", "media": [], "caption": ""}
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
    user_id = update.effective_user.id
    state = user_states.get(user_id)
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
        preview_lines.append(f"کپشن: {caption}")
    preview_lines.append("\nبه همه کانال‌ها ارسال شود؟")

    await msg.reply_text(
        "\n".join(preview_lines),
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )
    return True


async def handle_photo_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    return await _handle_media_item(update, context, "photo", photo.file_id)


async def handle_video_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video
    return await _handle_media_item(update, context, "video", video.file_id)


async def handle_text_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_states.get(user_id)
    if not state or state.get("state") != "awaiting_post":
        return False

    text = update.message.text
    state["type"] = "text"
    state["text"] = text
    state["state"] = "awaiting_confirm"
    state["message"] = update.message

    await update.message.reply_text(
        f"📝 <b>پیش‌نمایش پست:</b>\n\n{text}\n\nبه همه کانال‌ها ارسال شود؟",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )
    return True


async def handle_document_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_states.get(user_id)
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
        preview_lines.append(f"کپشن: {caption}")
    preview_lines.append(f"\n📎 فایل: {doc.file_name}")
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


async def handle_confirm_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    state = user_states.get(user_id)
    if not state or state.get("state") != "awaiting_confirm":
        await query.edit_message_text("❌ پستی در انتظار نیست. برای شروع /start را بزنید.")
        return

    tg_channels = await get_active_channels("telegram")
    bale_channels = await get_active_channels("bale")

    logger.info("Found %d Telegram channels, %d Bale channels", len(tg_channels), len(bale_channels))

    if not tg_channels and not bale_channels:
        await query.edit_message_text("❌ کانالی تنظیم نشده است. از تنظیمات کانال اضافه کنید.")
        user_states.pop(user_id, None)
        return

    await query.edit_message_text("⏳ در حال ارسال به کانال‌ها...")

    post_type = state.get("type")

    # Save to history first
    media_json = json.dumps(state.get("media")) if post_type == "media_group" else None
    post_id = await save_post(
        user_id, post_type,
        text=state.get("text"),
        file_id=state.get("file_id"),
        caption=state.get("caption"),
        media_json=media_json,
    )

    tg_sent, tg_failed, tg_message_ids = await _post_to_telegram(tg_channels, state, context.bot)
    bale_sent, bale_failed, bale_message_ids = await _post_to_bale(bale_channels, state, context.bot)

    # Update history with all message IDs
    all_message_ids = tg_message_ids + bale_message_ids
    await update_post_message_ids(post_id, json.dumps(all_message_ids), None)

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


async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route incoming media/text to the correct handler based on user state."""
    user_id = update.effective_user.id
    state = user_states.get(user_id)
    if not state:
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
