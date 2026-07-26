import json
import logging
import time

from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from database import (
    get_user_posts, get_all_posts, get_user_posts_paginated, get_all_posts_paginated,
    count_user_posts, count_all_posts, get_post, update_post_text, update_post_caption,
    delete_post, is_writer_or_above, is_owner, is_sudo, can_edit_post, can_delete_post,
    get_user_role, get_post, has_permission, update_post_status, save_post_version, save_post,
)
from keyboards import main_menu_keyboard, history_keyboard, post_detail_keyboard, confirm_keyboard
from utils import html_text, state_is_expired
from handlers.post import publish_existing_post, user_states

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


def _msg_text(text):
    """Try to send edit_message_text, fallback to reply_text."""
    pass


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

    post = await get_post(post_id)
    if not post:
        try:
            await query.edit_message_text("❌ پست یافت نشد.", reply_markup=await _menu_kb(query.from_user.id))
        except BadRequest:
            await query.message.reply_text("❌ پست یافت نشد.", reply_markup=await _menu_kb(query.from_user.id))
        return

    type_labels = {"text": "📝 متن", "photo": "🖼️ عکس", "video": "🎬 ویدیو", "document": "📎 فایل", "media_group": "📦 گروه رسانه"}
    label = type_labels.get(post["post_type"], post["post_type"])
    date = post["created_at"].strftime("%Y/%m/%d %H:%M") if post["created_at"] else ""

    status_labels = {"pending": "⏳ در انتظار", "draft": "💾 پیش‌نویس", "pending_approval": "🔐 در انتظار تأیید", "completed": "✅ کامل", "partial": "⚠️ ناقص", "failed": "❌ ناموفق"}
    delivery_status = status_labels.get(post.get("delivery_status"), "نامشخص")
    lines = [f"<b>{label}</b>", f"📅 {date}", f"📤 وضعیت ارسال: {delivery_status}", ""]
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
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=post_detail_keyboard(post_id, post.get("delivery_status", "completed")))
    except BadRequest:
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=post_detail_keyboard(post_id, post.get("delivery_status", "completed")))


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
    if not post or not await can_edit_post(query.from_user.id, post_id):
        await query.edit_message_text("❌ اجازه کپی این پست را ندارید.")
        return
    state = {"state": "awaiting_confirm", "type": post["post_type"], "text": post.get("text"),
             "file_id": post.get("file_id"), "caption": post.get("caption") or "", "media": _safe_parse_json(post.get("media_json")),
             "selected_channel_ids": _safe_parse_json(post.get("target_channels_json")), "created_at": time.monotonic()}
    user_states[query.from_user.id] = state
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
