import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import SUDO_USER_ID
from database import get_analytics, get_channel_health, get_active_channels, update_channel_health, is_sudo, is_owner
import bale_client

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
        f"🔐 در انتظار تأیید: {p.get('approvals', 0) or 0}\n"
        f"🕐 پست‌های ۲۴ ساعت اخیر: {data['last_24h']}\n\n"
        f"کانال‌ها: {c.get('active', 0) or 0}/{c.get('total', 0) or 0} فعال\n"
        f"پلتفرم‌ها: {platforms}"
    )


async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("❌ غیرمجاز.")
        return
    await update.message.reply_text(_analytics_text(await get_analytics()))


async def run_channel_health_checks(context: ContextTypes.DEFAULT_TYPE):
    channels = await get_channel_health()
    for channel in channels:
        if not channel["is_active"]:
            continue
        try:
            if channel["platform"] == "bale":
                result = await bale_client.get_chat(channel["chat_id"])
                if not result.get("ok"):
                    raise RuntimeError(result.get("description", "Bale API error"))
            else:
                await context.bot.get_chat(channel["chat_id"])
            await update_channel_health(channel["id"], "healthy")
        except Exception as exc:
            logger.warning("Channel health check failed for %s: %s", channel["name"], exc)
            await update_channel_health(channel["id"], "unhealthy", str(exc)[:1000])


async def handle_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("❌ غیرمجاز.")
        return
    await run_channel_health_checks(context)
    rows = await get_channel_health()
    if not rows:
        await update.message.reply_text("📋 کانالی تنظیم نشده است.")
        return
    lines = ["🩺 وضعیت کانال‌ها:"]
    for row in rows:
        icon = "✅" if row["last_health_status"] == "healthy" else "❌"
        lines.append(f"{icon} {row['name']} ({row['platform']}) — {row['last_health_status'] or 'نامشخص'}")
    await update.message.reply_text("\n".join(lines))


async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_message(chat_id=SUDO_USER_ID, text=_analytics_text(await get_analytics()))
    except Exception:
        logger.exception("Could not send daily report")
