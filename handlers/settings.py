import logging
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database import (
    add_channel,
    get_active_channels,
    is_owner,
    is_sudo,
    remove_channel, create_channel_group, get_channel_groups, get_setting, set_setting,
)
from keyboards import main_menu_keyboard, settings_markup, settings_main_markup
from utils import is_private_chat, state_is_expired
import bale_client

logger = logging.getLogger(__name__)

# Per-user state for channel input
_settings_states: dict[int, dict] = {}


async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_owner(query.from_user.id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    await query.edit_message_text(
        "⚙️ <b>تنظیمات ربات</b>\n\nیک بخش را انتخاب کنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_main_markup(is_sudo_user=await is_sudo(query.from_user.id)),
    )


async def handle_manage_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await is_owner(query.from_user.id):
        await query.edit_message_text("❌ غیرمجاز.")
        return
    channels = await get_active_channels()
    text = "📢 <b>مدیریت کانال‌ها</b>\n\n"
    text += "\n".join(f"• {ch['name']} ({ch['platform']}, {ch['chat_type']})" for ch in channels) if channels else "هنوز کانالی اضافه نشده است."
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=settings_markup(channels, is_sudo_user=await is_owner(query.from_user.id)))


async def handle_add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    # Buttons only hide an action, they do not protect it: callback_data can be
    # replayed by anyone, and a stale keyboard survives a demotion.
    if not await is_owner(user_id):
        await query.edit_message_text("❌ غیرمجاز.")
        return
    _settings_states[user_id] = {"state": "awaiting_channel_input", "platform": "telegram", "created_at": time.monotonic()}

    await query.edit_message_text(
        "➕ <b>افزودن کانال تلگرام</b>\n\n"
        "کانال یا گروه خود را ارسال کنید:\n"
        "• نام کاربری: @channelname\n"
        "• شناسه چت: -1001234567890\n\n"
        "ربات باید ادمین کانال/گروه باشد.",
        parse_mode=ParseMode.HTML,
    )


async def handle_add_bale_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not await is_owner(user_id):
        await query.edit_message_text("❌ غیرمجاز.")
        return
    _settings_states[user_id] = {"state": "awaiting_channel_input", "platform": "bale", "created_at": time.monotonic()}

    await query.edit_message_text(
        "➕ <b>افزودن کانال بله</b>\n\n"
        "کانال یا گروه خود را ارسال کنید:\n"
        "• نام کاربری: @channelname\n"
        "• شناسه چت: -1001234567890\n\n"
        "ربات باید ادمین کانال/گروه باشد.",
        parse_mode=ParseMode.HTML,
    )


async def handle_channel_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = _settings_states.get(user_id)
    if state and state_is_expired(state):
        _settings_states.pop(user_id, None)
        state = None
    if not state:
        return False
    if state_is_expired(state):
        _settings_states.pop(user_id, None)
        return False
    # Re-check at redemption time: the state may have been armed while the user
    # was still an owner, then redeemed after a demotion.
    if not await is_owner(user_id):
        _settings_states.pop(user_id, None)
        return False
    if state.get("state") == "awaiting_group_input":
        raw = update.message.text.strip() if update.message and update.message.text else ""
        if ":" not in raw:
            await update.message.reply_text("❌ فرمت نامعتبر است. نمونه: اخبار: 1,2,3")
            return True
        name, ids = raw.split(":", 1)
        try:
            channel_ids = [int(x.strip()) for x in ids.split(",") if x.strip()]
        except ValueError:
            await update.message.reply_text("❌ شناسه کانال نامعتبر است.")
            return True
        valid = {c["id"] for c in await get_active_channels()}
        channel_ids = [cid for cid in channel_ids if cid in valid]
        ok = bool(name.strip() and channel_ids) and await create_channel_group(user_id, name.strip(), channel_ids)
        _settings_states.pop(user_id, None)
        await update.message.reply_text("✅ گروه ساخته شد." if ok else "❌ گروه ساخته نشد؛ نام یا کانال‌ها را بررسی کنید.")
        return True
    if state.get("state") != "awaiting_channel_input":
        return False

    if not update.message or not update.message.text:
        return False

    text = update.message.text.strip()
    platform = state.pop("platform", "telegram")

    # Resolve the chat
    if platform == "bale":
        try:
            result = await bale_client.get_chat(text)
            if not result.get("ok"):
                raise Exception(result.get("description", "Unknown error"))
            chat = type("Chat", (), {
                "id": result["result"]["id"],
                "title": result["result"].get("title"),
                "username": result["result"].get("username"),
                "type": result["result"].get("type", "channel"),
            })()
        except Exception as e:
            await update.message.reply_text(
                f"❌ کانال/گروه بله یافت نشد: {e}\n\n"
                "مطمئن شوید:\n"
                "• نام کاربری/شناسه صحیح است\n"
                "• ربات بله به کانال/گروه اضافه شده\n\n"
                "از تنظیمات دوباره تلاش کنید.",
                reply_markup=main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id)),
            )
            return True
    else:
        try:
            chat = await context.bot.get_chat(text)
        except Exception:
            await update.message.reply_text(
                "❌ کانال/گروه یافت نشد. مطمئن شوید:\n"
                "• نام کاربری/شناسه صحیح است\n"
                "• ربات به کانال/گروه اضافه شده\n\n"
                "از تنظیمات دوباره تلاش کنید.",
                reply_markup=main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id)),
            )
            return True

    chat_type = "channel" if chat.type == "channel" else "group"
    name = chat.title or chat.username or str(chat.id)

    if await add_channel(chat.id, name, chat_type, platform):
        platform_label = "بله" if platform == "bale" else "تلگرام"
        await update.message.reply_text(
            f"✅ به {platform_label} اضافه شد: <b>{name}</b> ({chat_type})",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id)),
        )
    else:
        await update.message.reply_text(
            f"⚠️ <b>{name}</b> در لیست موجود است.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id)),
        )
    return True


async def handle_remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_owner(query.from_user.id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    try:
        channel_id = int(query.data.removeprefix("remove_"))
    except (IndexError, ValueError):
        return

    await remove_channel(channel_id)

    tg_channels = await get_active_channels("telegram")
    bale_channels = await get_active_channels("bale")

    text = "✅ کانال حذف شد.\n\n"
    if tg_channels:
        text += "📣 <b>کانال‌های تلگرام:</b>\n"
        for ch in tg_channels:
            text += f"  • {ch['name']} ({ch['chat_type']})\n"
    if bale_channels:
        text += "\n🔵 <b>کانال‌های بله:</b>\n"
        for ch in bale_channels:
            text += f"  • {ch['name']} ({ch['chat_type']})\n"
    if not tg_channels and not bale_channels:
        text += "📢 <b>کانالی تنظیم نشده است.</b>"

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=settings_markup(tg_channels + bale_channels, is_sudo_user=await is_owner(query.from_user.id)),
    )


async def handle_approval_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await is_owner(user_id):
        await query.answer("❌ فقط sudo یا owner دسترسی دارد.", show_alert=True)
        return
    enabled = (await get_setting("approval_required", "0")) == "1"
    status = "روشن ✅" if enabled else "خاموش ❌"
    from keyboards import approval_settings_keyboard
    await query.edit_message_text(
        f"🔐 <b>تأیید قبل از انتشار</b>\n\nوضعیت فعلی: <b>{status}</b>\n\n"
        "وقتی روشن باشد، پست‌های نویسندگان قبل از انتشار باید توسط sudo یا owner تأیید شوند.\n"
        "وقتی خاموش باشد، نویسندگان مستقیماً منتشر می‌کنند.",
        parse_mode=ParseMode.HTML,
        reply_markup=approval_settings_keyboard(enabled),
    )


async def handle_toggle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await is_owner(user_id):
        await query.answer("❌ فقط sudo یا owner دسترسی دارد.", show_alert=True)
        return
    current = (await get_setting("approval_required", "0")) == "1"
    await set_setting("approval_required", "0" if current else "1", user_id)
    await handle_approval_settings(update, context)


async def handle_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await is_owner(query.from_user.id):
        await query.answer("❌ فقط sudo یا owner دسترسی دارد.", show_alert=True)
        return
    from handlers.admin import run_channel_health_checks
    from database import get_channel_health
    await run_channel_health_checks(context)
    rows = await get_channel_health()
    if not rows:
        await query.answer("📋 کانالی تنظیم نشده است.", show_alert=True)
        return
    healthy = sum(1 for row in rows if row.get("last_health_status") == "healthy")
    lines = [f"🩺 وضعیت کانال‌ها: {healthy}/{len(rows)} سالم"]
    for row in rows:
        icon = "✅" if row.get("last_health_status") == "healthy" else "❌"
        lines.append(f"{icon} {row['name']} ({row['platform']}): {row.get('last_health_status') or 'نامشخص'}")
    await query.answer("\n".join(lines)[:1900], show_alert=True)


async def handle_bot_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    logger.info("[RESTART] Sudo user %s requested bot restart confirmation", query.from_user.id)

    if not await is_sudo(query.from_user.id):
        logger.warning("[RESTART] Unauthorized restart request by user %s", query.from_user.id)
        await query.answer("❌ فقط ادمین اصلی (Sudo) دسترسی دارد.", show_alert=True)
        return

    from keyboards import restart_confirm_keyboard
    await query.edit_message_text(
        "⚠️ <b>تأیید ری‌استارت</b>\n\nآیا از ری‌استارت کردن ربات اطمینان دارید؟",
        parse_mode=ParseMode.HTML,
        reply_markup=restart_confirm_keyboard()
    )


async def handle_do_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    logger.info("[RESTART] Sudo user %s confirmed bot restart", query.from_user.id)

    if not await is_sudo(query.from_user.id):
        logger.warning("[RESTART] Unauthorized do_restart request by user %s", query.from_user.id)
        await query.answer("❌ فقط ادمین اصلی (Sudo) دسترسی دارد.", show_alert=True)
        return

    from config import RESTART_DRAIN_TIMEOUT_SECONDS
    from handlers.post import (
        user_states, persist_state, inflight_count, wait_for_inflight,
    )

    # 1. Flush every in-memory workflow to the database so nobody loses the
    #    post they are composing. The restart handler used to do none of this.
    saved = 0
    for uid, state in list(user_states.items()):
        try:
            await persist_state(uid, state)
            saved += 1
        except Exception:
            logger.exception("[RESTART] Could not persist workflow for %s", uid)

    # 2. Tell everyone mid-workflow what is happening, so their next message is
    #    not swallowed silently by a bot that is going down.
    notified = 0
    for uid in list(user_states.keys()):
        if uid == query.from_user.id:
            continue
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="⏸ ربات برای چند لحظه ری‌استارت می‌شود.\n"
                     "کار شما ذخیره شد و پس از بازگشت ادامه خواهد داشت. "
                     "لطفاً تا اعلام آماده‌باش چیزی نفرستید.",
            )
            notified += 1
        except Exception:
            logger.debug("[RESTART] Could not notify %s", uid, exc_info=True)

    # 3. Wait for in-flight publishes. Restarting mid-broadcast would leave a
    #    post delivered to some channels and not others.
    pending = await inflight_count()
    if pending:
        await query.edit_message_text(
            f"⏳ <b>در انتظار اتمام {pending} ارسال در حال انجام...</b>",
            parse_mode=ParseMode.HTML,
        )
        drained = await wait_for_inflight(RESTART_DRAIN_TIMEOUT_SECONDS)
        if not drained:
            remaining = await inflight_count()
            logger.warning("[RESTART] Proceeding with %d publish(es) still in flight", remaining)
            await query.edit_message_text(
                f"⚠️ <b>{remaining} ارسال هنوز تمام نشده است.</b>\n"
                "ری‌استارت ادامه می‌یابد؛ این پست‌ها پس از بازگشت بازیابی می‌شوند.",
                parse_mode=ParseMode.HTML,
            )

    logger.info("[RESTART] Drained. %d workflow(s) saved, %d user(s) notified", saved, notified)
    await query.edit_message_text(
        f"🔄 <b>در حال ری‌استارت ربات...</b>\n"
        f"💾 {saved} عملیات در حال انجام ذخیره شد.\n"
        "لطفاً چند لحظه صبر کنید.",
        parse_mode=ParseMode.HTML,
    )

    def _do_restart():
        import time
        time.sleep(1)
        try:
            import subprocess
            import os
            config_path = os.path.expanduser("~/supervisord.conf")
            cmd = ["supervisorctl", "-c", config_path, "restart", "tisa_manager"]
            logger.info("[RESTART] Starting supervisor restart with config: %s", config_path)
            
            res = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=15
            )
            logger.info("[RESTART] Supervisor restart completed: stdout=%s | stderr=%s", res.stdout, res.stderr)
        except Exception as e:
            logger.error("[RESTART] Supervisor restart failed with custom config: %s. Trying fallback...", e)
            try:
                res = subprocess.run(
                    ["supervisorctl", "restart", "tisa_manager"],
                    capture_output=True, text=True, timeout=15
                )
                logger.info("[RESTART] Fallback supervisor restart completed: stdout=%s | stderr=%s", res.stdout, res.stderr)
            except Exception as e2:
                logger.error("[RESTART] Fallback supervisor restart also failed: %s", e2, exc_info=True)

    import threading
    threading.Thread(target=_do_restart, daemon=True).start()


async def handle_groups_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await is_owner(user_id):
        await query.edit_message_text("❌ غیرمجاز.")
        return
    groups = await get_channel_groups(user_id)
    text = "📋 گروه‌های کانال:\n" + ("\n".join(f"• {g['name']} ({g['channel_count']} کانال)" for g in groups) if groups else "هنوز گروهی ساخته نشده است.")
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ ساخت گروه", callback_data="create_group")],
        [InlineKeyboardButton("◀️ ابزارها", callback_data="tools_menu")],
    ]))


async def handle_create_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await is_owner(query.from_user.id):
        await query.edit_message_text("❌ غیرمجاز.")
        return
    _settings_states[query.from_user.id] = {"state": "awaiting_group_input", "created_at": time.monotonic()}
    await query.edit_message_text("نام گروه و شناسه کانال‌ها را به شکل زیر ارسال کنید:\nنام گروه: 1,2,3")


async def handle_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from handlers.post import cancel_all_workflows
    cancel_all_workflows(update.effective_user.id)
    user_id = update.effective_user.id
    if not await is_owner(user_id):
        await update.message.reply_text("❌ غیرمجاز.")
        return
    parts = (update.message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("فرمت: /group نام_گروه شناسه_کانال‌ها\nمثال: /group اخبار 1,2,3")
        return
    name, raw_ids = parts[1], parts[2]
    try:
        channel_ids = [int(x.strip()) for x in raw_ids.split(",") if x.strip()]
    except ValueError:
        await update.message.reply_text("❌ شناسه کانال نامعتبر است.")
        return
    valid = {c["id"] for c in await get_active_channels()}
    channel_ids = [cid for cid in channel_ids if cid in valid]
    if not channel_ids:
        await update.message.reply_text("❌ هیچ کانال فعالی انتخاب نشده است.")
        return
    ok = await create_channel_group(user_id, name, channel_ids)
    await update.message.reply_text("✅ گروه کانال ساخته شد." if ok else "⚠️ این نام گروه قبلاً وجود دارد.")


async def handle_groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from handlers.post import cancel_all_workflows
    cancel_all_workflows(update.effective_user.id)
    user_id = update.effective_user.id
    if not await is_owner(user_id):
        await update.message.reply_text("❌ غیرمجاز.")
        return
    groups = await get_channel_groups(user_id)
    if not groups:
        await update.message.reply_text("📋 گروه کانالی وجود ندارد.")
        return
    text = "📋 گروه‌های کانال:\n" + "\n".join(f"• {g['name']} ({g['channel_count']} کانال)" for g in groups)
    await update.message.reply_text(text)


async def handle_back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # "Back to main menu" is the pivot that turned a group /help into the full
    # admin menu; never render that keyboard outside a private chat.
    if not is_private_chat(update):
        return
    user_id = query.from_user.id
    await query.edit_message_text(
        "🤖 ربات مدیریت پست\n\nیک گزینه را انتخاب کنید:",
        reply_markup=main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id)),
    )
