import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import SUDO_USER_ID
from database import get_analytics, get_channel_health, get_active_channels, update_channel_health, is_sudo, is_owner
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import bale_client
from handlers.post import cancel_all_workflows

logger = logging.getLogger(__name__)


def _analytics_text(data):
    p = data["posts"] or {}
    c = data["channels"] or {}
    platforms = " | ".join(f"{x['platform']}: {x['total']}" for x in data["platforms"]) or "—"
    return (
        "📊 گزارش عملکرد ربات\n\n"
        f"پست‌ها: {p.get('total', 0) or 0}\n"
        f"✅ کامل: {p.get('completed', 0) or 0}\n"
        f"⚠️ ناقص: {p.get('partial', 0) or 0}\n"
        f"❌ ناموفق: {p.get('failed', 0) or 0}\n"
        f"💾 پیش‌نویس: {p.get('drafts', 0) or 0}\n"
        f"🕒 زمان‌بندی‌شده: {p.get('scheduled', 0) or 0}\n"
        f"🔐 در انتظار تأیید: {p.get('approvals', 0) or 0}\n"
        f"🔁 در صف تلاش مجدد: {data.get('pending_retries', 0) or 0}\n"
        f"🕐 پست‌های ۲۴ ساعت اخیر: {data['last_24h']}\n\n"
        f"کانال‌ها: {c.get('active', 0) or 0}/{c.get('total', 0) or 0} فعال\n"
        f"پلتفرم‌ها: {platforms}"
    )


async def handle_tools_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🧰 ابزارها را انتخاب کنید:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار", callback_data="tools_stats"), InlineKeyboardButton("🩺 سلامت کانال‌ها", callback_data="tools_health")],
        [InlineKeyboardButton("📋 گروه‌های کانال", callback_data="tools_groups")],
        [InlineKeyboardButton("◀️ منوی اصلی", callback_data="back_main")],
    ]))


async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_all_workflows(update.effective_user.id)
    if not await is_owner(update.effective_user.id):
        if update.callback_query:
            await update.callback_query.answer("❌ غیرمجاز.", show_alert=True)
        else:
            await update.message.reply_text("❌ غیرمجاز.")
        return
    text = _analytics_text(await get_analytics())
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ ابزارها", callback_data="tools_menu")]]))
    else:
        await update.message.reply_text(text)


async def run_channel_health_checks(context: ContextTypes.DEFAULT_TYPE):
    channels = [channel for channel in await get_channel_health() if channel["is_active"]]
    semaphore = asyncio.Semaphore(10)

    async def check(channel):
        async with semaphore:
            try:
                if channel["platform"] == "bale":
                    result = await asyncio.wait_for(bale_client.get_chat(channel["chat_id"]), timeout=20)
                    if not result.get("ok"):
                        raise RuntimeError(result.get("description", "Bale API error"))
                else:
                    await asyncio.wait_for(context.bot.get_chat(channel["chat_id"]), timeout=20)
                await update_channel_health(channel["id"], "healthy")
            except Exception as exc:
                logger.warning("Channel health check failed for %s: %s", channel["name"], exc)
                await update_channel_health(channel["id"], "unhealthy", str(exc)[:1000])

    await asyncio.gather(*(check(channel) for channel in channels))


async def handle_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_all_workflows(update.effective_user.id)
    if not await is_owner(update.effective_user.id):
        if update.callback_query:
            await update.callback_query.answer("❌ غیرمجاز.", show_alert=True)
        else:
            await update.message.reply_text("❌ غیرمجاز.")
        return
    await run_channel_health_checks(context)
    rows = await get_channel_health()
    if not rows:
        if update.callback_query:
            await update.callback_query.edit_message_text("📋 کانالی تنظیم نشده است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ ابزارها", callback_data="tools_menu")]]))
        else:
            await update.message.reply_text("📋 کانالی تنظیم نشده است.")
        return
    lines = ["🩺 وضعیت کانال‌ها:"]
    for row in rows:
        icon = "✅" if row["last_health_status"] == "healthy" else "❌"
        lines.append(f"{icon} {row['name']} ({row['platform']}) — {row['last_health_status'] or 'نامشخص'}")
    if update.callback_query:
        await update.callback_query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ ابزارها", callback_data="tools_menu")]]))
    else:
        await update.message.reply_text("\n".join(lines))


async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_message(chat_id=SUDO_USER_ID, text=_analytics_text(await get_analytics()))
    except Exception:
        logger.exception("Could not send daily report")
