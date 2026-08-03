"""Managing already-created schedules: list, inspect, reschedule, cancel.

Before this existed a schedule was fire-and-forget — once created there was no
way to see it, move it or stop it, and /cancel only cleared in-memory state
while the database row still fired.
"""

import logging
import time
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from database import (
    get_pending_schedules, get_schedule, cancel_schedule, reschedule,
    claim_schedule, update_schedule, get_post, is_sudo, is_owner,
    is_writer_or_above, get_user_role, update_post_status, update_post_delivery,
)
from keyboards import (
    main_menu_keyboard, scheduled_list_keyboard, scheduled_detail_keyboard,
    schedule_date_keyboard,
)
from utils import (
    html_text, format_local, format_local_date, now_local, local_to_utc_naive,
    date_example, parse_user_datetime, private_actor,
)

logger = logging.getLogger(__name__)

# user_id -> {"schedule_id": int, "created_at": monotonic}
_reschedule_states: dict[int, dict] = {}
_RESCHEDULE_TTL = 15 * 60

_TYPE_LABELS = {
    "text": "📝 متن", "photo": "🖼️ عکس", "video": "🎬 ویدیو",
    "document": "📎 فایل", "media_group": "📦 گروه رسانه",
}
_STATUS_LABELS = {
    "scheduled": "🕒 در صف",
    "processing": "🔄 در حال ارسال",
    "completed": "✅ انجام شد",
    "failed": "❌ ناموفق",
    "cancelled": "🚫 لغو شده",
    "expired": "⏰ منقضی شده",
}


async def _menu_kb(user_id):
    return main_menu_keyboard(is_sudo=await is_sudo(user_id), is_owner=await is_owner(user_id))


async def _safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except BadRequest:
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


async def _may_manage(user_id: int, schedule: dict) -> bool:
    """Owners/sudo manage everything; writers only their own schedules."""
    if schedule is None:
        return False
    role = await get_user_role(user_id)
    if role in ("sudo", "owner"):
        return True
    return schedule["user_id"] == user_id


async def handle_scheduled_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await is_writer_or_above(user_id):
        await _safe_edit(query, "❌ غیرمجاز.")
        return

    role = await get_user_role(user_id)
    schedules = await get_pending_schedules(None if role in ("sudo", "owner") else user_id)

    if not schedules:
        await _safe_edit(
            query,
            "🕒 <b>پست‌های زمان‌بندی‌شده</b>\n\nهیچ پست زمان‌بندی‌شده‌ای وجود ندارد.",
            reply_markup=await _menu_kb(user_id),
        )
        return

    lines = ["🕒 <b>پست‌های زمان‌بندی‌شده</b>", ""]
    for s in schedules:
        lines.append(
            f"• #{s['id']} — {format_local(s['run_at'])} — {_STATUS_LABELS.get(s['status'], s['status'])}"
        )
    lines.append("\nبرای مدیریت، یکی را انتخاب کنید:")
    await _safe_edit(
        query, "\n".join(lines),
        reply_markup=scheduled_list_keyboard(schedules, format_local),
    )


async def handle_schedule_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        schedule_id = int(query.data.removeprefix("sched_view_"))
    except ValueError:
        return
    schedule = await get_schedule(schedule_id)
    if not schedule:
        await _safe_edit(query, "❌ زمان‌بندی یافت نشد.", reply_markup=await _menu_kb(query.from_user.id))
        return
    if not await _may_manage(query.from_user.id, schedule):
        await query.answer("❌ اجازه مدیریت این زمان‌بندی را ندارید.", show_alert=True)
        return

    post = await get_post(schedule["post_id"])
    label = _TYPE_LABELS.get(post["post_type"], post["post_type"]) if post else "—"
    preview = ""
    if post:
        preview = (post.get("text") or post.get("caption") or "").strip()
        if len(preview) > 300:
            preview = preview[:300] + "…"

    lines = [
        f"🕒 <b>زمان‌بندی #{schedule['id']}</b>",
        f"وضعیت: {_STATUS_LABELS.get(schedule['status'], schedule['status'])}",
        f"زمان انتشار: <b>{format_local(schedule['run_at'])}</b>",
        f"پست: #{schedule['post_id']} ({label})",
    ]
    if schedule.get("attempts"):
        lines.append(f"تلاش‌ها: {schedule['attempts']}")
    if preview:
        lines.append(f"\n{html_text(preview)}")
    if schedule["status"] == "processing":
        lines.append("\n🔄 این پست هم‌اکنون در حال ارسال است و قابل تغییر نیست.")

    await _safe_edit(
        query, "\n".join(lines),
        reply_markup=scheduled_detail_keyboard(
            schedule["id"], schedule["post_id"], locked=schedule["status"] == "processing",
        ),
    )


async def handle_schedule_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        schedule_id = int(query.data.removeprefix("sched_cancel_"))
    except ValueError:
        return
    schedule = await get_schedule(schedule_id)
    if not await _may_manage(query.from_user.id, schedule):
        await query.answer("❌ اجازه مدیریت این زمان‌بندی را ندارید.", show_alert=True)
        return

    # cancel_schedule only touches rows still in 'scheduled', so a publish that
    # has already been claimed cannot be torn out from under the worker.
    if not await cancel_schedule(schedule_id):
        await query.answer(
            "❌ این زمان‌بندی هم‌اکنون در حال ارسال است یا قبلاً پردازش شده.", show_alert=True,
        )
        return

    # The post stays in history as a draft the author can reuse.
    await update_post_status(schedule["post_id"], "draft")
    await _safe_edit(
        query,
        f"🚫 زمان‌بندی #{schedule_id} لغو شد.\n"
        f"پست #{schedule['post_id']} به‌صورت پیش‌نویس در تاریخچه باقی ماند.",
        reply_markup=await _menu_kb(query.from_user.id),
    )


async def handle_schedule_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Publish a scheduled post immediately.

    Claims the row first, so the periodic job cannot also pick it up and send
    the post a second time.
    """
    from handlers.post import publish_existing_post

    query = update.callback_query
    await query.answer()
    try:
        schedule_id = int(query.data.removeprefix("sched_now_"))
    except ValueError:
        return
    schedule = await get_schedule(schedule_id)
    if not await _may_manage(query.from_user.id, schedule):
        await query.answer("❌ اجازه مدیریت این زمان‌بندی را ندارید.", show_alert=True)
        return

    if not await claim_schedule(schedule_id):
        await query.answer("❌ این زمان‌بندی هم‌اکنون در حال ارسال است.", show_alert=True)
        return

    post = await get_post(schedule["post_id"])
    if not post:
        await update_schedule(schedule_id, "failed", "post not found")
        await _safe_edit(query, "❌ پست یافت نشد.", reply_markup=await _menu_kb(query.from_user.id))
        return

    await _safe_edit(query, "⏳ در حال ارسال فوری...")
    try:
        await update_post_status(post["id"], "pending")
        sent, failed = await publish_existing_post(post, context.bot)
        await update_schedule(schedule_id, "completed" if not failed else "failed",
                              None if not failed else f"{failed} channel(s) failed")
        text = f"✅ ارسال شد: {sent} موفق، {failed} ناموفق."
    except Exception as exc:
        logger.exception("Immediate publish of schedule %s failed", schedule_id)
        await update_schedule(schedule_id, "failed", str(exc)[:1000])
        text = f"❌ ارسال ناموفق بود: {html_text(str(exc)[:200])}"

    await query.message.reply_text(text, parse_mode=ParseMode.HTML,
                                   reply_markup=await _menu_kb(query.from_user.id))


async def handle_schedule_time_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the reschedule flow — pick a new date for an existing schedule."""
    query = update.callback_query
    await query.answer()
    try:
        schedule_id = int(query.data.removeprefix("sched_time_"))
    except ValueError:
        return
    schedule = await get_schedule(schedule_id)
    if not await _may_manage(query.from_user.id, schedule):
        await query.answer("❌ اجازه مدیریت این زمان‌بندی را ندارید.", show_alert=True)
        return
    if schedule["status"] != "scheduled":
        await query.answer("❌ فقط زمان‌بندی‌های در صف قابل تغییرند.", show_alert=True)
        return

    _reschedule_states[query.from_user.id] = {
        "schedule_id": schedule_id, "created_at": time.monotonic(),
    }
    await _safe_edit(
        query,
        f"🕒 <b>تغییر زمان زمان‌بندی #{schedule_id}</b>\n\n"
        f"زمان فعلی: {format_local(schedule['run_at'])}\n\n"
        f"زمان جدید را بفرستید، نمونه:\n<code>{html_text(date_example())}</code>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ بازگشت", callback_data=f"sched_view_{schedule_id}")],
        ]),
    )


async def handle_reschedule_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Consume 'YYYY-MM-DD HH:MM' while a reschedule is pending."""
    actor = private_actor(update)
    if actor is None:
        return False
    user_id = actor.id
    state = _reschedule_states.get(user_id)
    if not state:
        return False
    if time.monotonic() - state["created_at"] > _RESCHEDULE_TTL:
        _reschedule_states.pop(user_id, None)
        return False
    if not update.message.text:
        return False

    try:
        local_time = parse_user_datetime(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(
            f"❌ {html_text(exc)}\n\nنمونه درست:\n<code>{html_text(date_example())}</code>",
            parse_mode=ParseMode.HTML,
        )
        return True

    if local_time <= now_local().replace(tzinfo=None):
        await update.message.reply_text("❌ این زمان گذشته است. زمانی در آینده بفرستید.")
        return True

    schedule_id = state["schedule_id"]
    schedule = await get_schedule(schedule_id)
    if not await _may_manage(user_id, schedule):
        _reschedule_states.pop(user_id, None)
        await update.message.reply_text("❌ اجازه مدیریت این زمان‌بندی را ندارید.")
        return True

    if not await reschedule(schedule_id, local_to_utc_naive(local_time)):
        _reschedule_states.pop(user_id, None)
        await update.message.reply_text(
            "❌ زمان‌بندی تغییر نکرد؛ ممکن است هم‌اکنون در حال ارسال باشد."
        )
        return True

    _reschedule_states.pop(user_id, None)
    await update.message.reply_text(
        f"✅ زمان‌بندی #{schedule_id} به "
        f"{format_local_date(local_time.date(), long_form=True)} "
        f"ساعت {local_time:%H:%M} منتقل شد.",
        reply_markup=await _menu_kb(user_id),
    )
    return True


def cancel_reschedule(user_id: int):
    _reschedule_states.pop(user_id, None)
