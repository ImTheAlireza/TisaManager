import asyncio
import json
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from config import SUDO_USER_ID
from database import (
    get_channel_health, get_active_channels, update_channel_health, is_sudo, is_owner,
    get_setting, set_setting,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from utils import html_text, format_local
import bale_client
from handlers.post import cancel_all_workflows

logger = logging.getLogger(__name__)

# Where the last bot-level health result lives (bot_settings key).
BOT_HEALTH_SETTING = "bot_health"

_STATUS_LABELS = {
    "healthy": "✅ سالم",
    "degraded": "⚠️ نیمه‌سالم",
    "unhealthy": "❌ ناسالم",
}


async def handle_tools_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = (
        "🧰 <b>ابزارها</b>\n\n"
        "یکی از بخش‌های زیر را انتخاب کنید:"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار و گزارش‌ها", callback_data="tools_stats")],
        [InlineKeyboardButton("🩺 سلامت ربات‌ها و کانال‌ها", callback_data="tools_health")],
        [InlineKeyboardButton("📋 گروه‌های کانال", callback_data="tools_groups")],
        [InlineKeyboardButton("◀️ بازگشت به منوی اصلی", callback_data="back_main")],
    ]))


def classify_bale_channel(errors: list, total_clients: int) -> tuple:
    """(status, error_text) for a Bale channel given per-bot failures.

    healthy: every configured bot reaches the channel.
    degraded: at least one bot reaches it — posts still go out, because
              attempts alternate bots, but every other attempt fails.
    unhealthy: no bot reaches the channel.
    """
    if not errors:
        return "healthy", None
    text = "؛ ".join(errors)
    if len(errors) < total_clients:
        return "degraded", text
    return "unhealthy", text


async def run_channel_health_checks(context: ContextTypes.DEFAULT_TYPE):
    channels = [channel for channel in await get_channel_health() if channel["is_active"]]
    semaphore = asyncio.Semaphore(10)

    async def check(channel):
        async with semaphore:
            try:
                if channel["platform"] == "bale":
                    # Delivery attempts alternate between the configured Bale
                    # bots, so every bot must be able to reach the channel.
                    clients = bale_client.all_clients()
                    if not clients:
                        raise RuntimeError("no Bale bot configured")
                    errors = []
                    for client in clients:
                        result = await asyncio.wait_for(client.get_chat(channel["chat_id"]), timeout=20)
                        if not result.get("ok"):
                            errors.append(f"{client.name}: {result.get('description', 'Bale API error')}")
                    status, error_text = classify_bale_channel(errors, len(clients))
                    await update_channel_health(channel["id"], status,
                                                error_text[:1000] if error_text else None)
                    if status != "healthy":
                        logger.warning("Channel %s is %s: %s", channel["name"], status, error_text)
                else:
                    await asyncio.wait_for(context.bot.get_chat(channel["chat_id"]), timeout=20)
                    await update_channel_health(channel["id"], "healthy")
            except Exception as exc:
                logger.warning("Channel health check failed for %s: %s", channel["name"], exc)
                await update_channel_health(channel["id"], "unhealthy", str(exc)[:1000])

    await asyncio.gather(*(check(channel) for channel in channels))


async def run_bot_health_checks(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Check the bots themselves (getMe) and return a report dict.

    Covers the Telegram bot and every configured Bale bot, so a dead token or
    an unreachable Bale API shows up before it costs failed deliveries.
    """
    results = {}

    try:
        me = await asyncio.wait_for(context.bot.get_me(), timeout=20)
        name = f"@{me.username}" if getattr(me, "username", None) else (me.full_name or "telegram")
        results["telegram"] = {"ok": True, "name": name}
    except Exception as exc:
        results["telegram"] = {"ok": False, "error": str(exc)[:200]}

    for client in bale_client.all_clients():
        try:
            res = await asyncio.wait_for(client.get_me(), timeout=20)
            if res.get("ok"):
                r = res.get("result") or {}
                username = r.get("username")
                name = f"@{username}" if username else (r.get("first_name") or client.name)
                results[client.name] = {"ok": True, "name": name}
            else:
                results[client.name] = {"ok": False,
                                        "error": str(res.get("description", "Bale API error"))[:200]}
        except Exception as exc:
            results[client.name] = {"ok": False, "error": str(exc)[:200]}

    results["checked_at"] = datetime.utcnow().isoformat()
    return results


def load_bot_health(raw) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def _health_text(rows: list, bot_health: dict | None, backup_configured: bool) -> str:
    """Render the health dashboard (pure function, easy to test)."""
    lines = ["🩺 <b>سلامت ربات‌ها و کانال‌ها</b>"]

    checked_at = (bot_health or {}).get("checked_at")
    if checked_at:
        try:
            when = format_local(datetime.fromisoformat(checked_at))
        except ValueError:
            when = None
        lines.append(f"🕒 آخرین بررسی: {when}" if when else "🕒 آخرین بررسی: نامشخص")
    else:
        lines.append("🕒 هنوز بررسی‌ای انجام نشده است.")
    lines.append("")

    # --- Bots -------------------------------------------------------------
    lines.append("🤖 <b>ربات‌ها</b>")
    if bot_health is None:
        lines.append("⚪ اطلاعاتی ثبت نشده — «🔄 بررسی مجدد» را بزنید.")
    else:
        tg = bot_health.get("telegram")
        if tg and tg.get("ok"):
            lines.append(f"✅ ربات تلگرام: {html_text(tg.get('name', ''))}")
        elif tg:
            lines.append(f"❌ ربات تلگرام: {html_text(tg.get('error', 'خطا'))}")
        bale_1 = bot_health.get("bale-1")
        if bale_1 and bale_1.get("ok"):
            lines.append(f"✅ ربات بله ۱: {html_text(bale_1.get('name', ''))}")
        elif bale_1:
            lines.append(f"❌ ربات بله ۱: {html_text(bale_1.get('error', 'خطا'))}")
        if backup_configured:
            bale_2 = bot_health.get("bale-2")
            if bale_2 and bale_2.get("ok"):
                lines.append(f"✅ ربات بله ۲: {html_text(bale_2.get('name', ''))}")
            elif bale_2:
                lines.append(f"❌ ربات بله ۲: {html_text(bale_2.get('error', 'خطا'))}")
            else:
                lines.append("⚪ ربات بله ۲: نتیجه‌ای ثبت نشده")
        else:
            lines.append("⚪ ربات بله ۲: پیکربندی نشده (BALE_TOKEN_2)")
    lines.append("")

    # --- Channels -----------------------------------------------------------
    active = [r for r in rows if r["is_active"]]
    lines.append(f"📢 <b>کانال‌ها</b> ({len(active)} فعال)")
    if not rows:
        lines.append("⚪ کانالی تنظیم نشده است.")
    else:
        for row in rows:
            icon = "🔵" if row["platform"] == "bale" else "📣"
            status = row.get("last_health_status")
            if not row["is_active"]:
                lines.append(f"{icon} {html_text(row['name'])} — ⏸️ غیرفعال")
                continue
            if status is None:
                lines.append(f"{icon} {html_text(row['name'])} — ⚪ بررسی نشده")
                continue
            label = _STATUS_LABELS.get(status, status)
            lines.append(f"{icon} {html_text(row['name'])} — {label}")
            if row.get("last_health_error") and status != "healthy":
                lines.append(f"   ↳ <code>{html_text(row['last_health_error'][:200])}</code>")
            if row.get("last_health_check"):
                lines.append(f"   🕒 {format_local(row['last_health_check'])}")

    return "\n".join(lines)


def _health_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 بررسی مجدد", callback_data="health_refresh")],
        [InlineKeyboardButton("◀️ بازگشت به ابزارها", callback_data="tools_menu")],
    ])


async def _render_health(update, context, refresh: bool):
    """Show the dashboard; with ``refresh`` run every check first."""
    if refresh:
        try:
            bot_health = await run_bot_health_checks(context)
            await set_setting(BOT_HEALTH_SETTING, json.dumps(bot_health, ensure_ascii=False), SUDO_USER_ID)
        except Exception:
            logger.exception("Bot health checks failed")
            bot_health = load_bot_health(await get_setting(BOT_HEALTH_SETTING))
        try:
            await run_channel_health_checks(context)
        except Exception:
            logger.exception("Channel health checks failed")

    rows = await get_channel_health()
    bot_health = load_bot_health(await get_setting(BOT_HEALTH_SETTING))
    text = _health_text(rows, bot_health, bale_client.BACKUP_CLIENT is not None)
    markup = _health_keyboard()

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def handle_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open the health dashboard (``/health`` or the tools-menu button).

    Renders the stored results instantly; if nothing was ever checked, the
    first visit runs a full check automatically.
    """
    cancel_all_workflows(update.effective_user.id)
    if not await is_owner(update.effective_user.id):
        if update.callback_query:
            await update.callback_query.answer("❌ غیرمجاز.", show_alert=True)
        else:
            await update.message.reply_text("❌ غیرمجاز.")
        return
    if update.callback_query:
        await update.callback_query.answer()

    never_checked = (await get_setting(BOT_HEALTH_SETTING)) is None
    if never_checked:
        rows = await get_channel_health()
        never_checked = not any(r.get("last_health_check") for r in rows)
        if update.callback_query:
            await update.callback_query.edit_message_text("⏳ در حال بررسی سلامت...")
    await _render_health(update, context, refresh=never_checked)


async def handle_health_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The 🔄 button: re-run every bot and channel check, then re-render."""
    if not await is_owner(update.effective_user.id):
        await update.callback_query.answer("❌ غیرمجاز.", show_alert=True)
        return
    await update.callback_query.answer("⏳ در حال بررسی...")
    await update.callback_query.edit_message_text("⏳ در حال بررسی سلامت ربات‌ها و کانال‌ها...")
    await _render_health(update, context, refresh=True)
