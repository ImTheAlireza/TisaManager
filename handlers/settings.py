import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database import (
    add_channel,
    get_active_channels,
    is_owner,
    is_sudo,
    remove_channel,
)
from keyboards import main_menu_keyboard, settings_markup
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

    tg_channels = await get_active_channels("telegram")
    bale_channels = await get_active_channels("bale")

    text = ""
    if tg_channels:
        text += "📣 <b>کانال‌های تلگرام:</b>\n"
        for ch in tg_channels:
            text += f"  • {ch['name']} ({ch['chat_type']})\n"
    if bale_channels:
        text += "\n🔵 <b>کانال‌های بله:</b>\n"
        for ch in bale_channels:
            text += f"  • {ch['name']} ({ch['chat_type']})\n"
    if not tg_channels and not bale_channels:
        text = "📢 <b>کانالی تنظیم نشده است.</b>\n\nبرای شروع ارسال، کانال اضافه کنید."

    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=settings_markup(tg_channels + bale_channels, is_sudo_user=await is_sudo(query.from_user.id)))


async def handle_add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    _settings_states[user_id] = {"state": "awaiting_channel_input", "platform": "telegram"}

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
    _settings_states[user_id] = {"state": "awaiting_channel_input", "platform": "bale"}

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
    if not state or state.get("state") != "awaiting_channel_input":
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
        reply_markup=settings_markup(tg_channels + bale_channels, is_sudo_user=await is_sudo(query.from_user.id)),
    )


async def handle_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    logger.info("[STATUS] Sudo user %s requested bot status", query.from_user.id)

    if not await is_sudo(query.from_user.id):
        logger.warning("[STATUS] Unauthorized status request by user %s", query.from_user.id)
        await query.answer("❌ فقط ادمین اصلی (Sudo) دسترسی دارد.", show_alert=True)
        return

    try:
        import subprocess
        import os
        config_path = os.path.expanduser("~/supervisord.conf")
        cmd = ["supervisorctl", "-c", config_path, "status", "tisa_manager"]
        logger.info("[STATUS] Attempting supervisorctl status with config: %s", config_path)
        
        res = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=5
        )
        output = res.stdout.strip() or res.stderr.strip() or "بدون خروجی"
        logger.info("[STATUS] Success: stdout=%s | stderr=%s", res.stdout, res.stderr)
    except Exception as e:
        logger.error("[STATUS] Failed: %s", e, exc_info=True)
        # Fallback without explicit config path if supervisord.conf is default or elsewhere
        try:
            logger.info("[STATUS] Trying fallback supervisorctl status without custom config")
            res = subprocess.run(
                ["supervisorctl", "status", "tisa_manager"],
                capture_output=True, text=True, timeout=5
            )
            output = res.stdout.strip() or res.stderr.strip() or "بدون خروجی"
            logger.info("[STATUS] Fallback success: stdout=%s | stderr=%s", res.stdout, res.stderr)
        except Exception as e2:
            logger.error("[STATUS] Fallback also failed: %s", e2, exc_info=True)
            output = f"خطا در اجرای دستور: {e}"

    if len(output) > 200:
        output = output[:200] + "..."
    await query.answer(f"📊 وضعیت:\n{output}", show_alert=True)


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

    await query.edit_message_text("🔄 <b>در حال ری‌استارت ربات...</b>\nلطفاً چند لحظه صبر کنید.", parse_mode=ParseMode.HTML)

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


async def handle_back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    await query.edit_message_text(
        "🤖 ربات مدیریت پست\n\nیک گزینه را انتخاب کنید:",
        reply_markup=main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id)),
    )
