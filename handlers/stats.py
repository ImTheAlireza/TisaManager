"""Analytics screens.

Replaces the single flat wall of all-time counters with a small menu. Every
number is either scoped to a window (so it means something) or paired with the
previous period (so it has context).

Access is scoped: owners and sudo see everything; a writer sees only their own
posts. That is enforced here by passing ``user_id`` into the queries rather
than by hiding buttons, because callback data can be replayed.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from database import (
    get_analytics, get_daily_counts, get_hourly_distribution, get_channel_stats,
    get_author_stats, get_schedule_stats, get_failure_breakdown,
    get_channel_health, is_owner, is_writer_or_above, get_user_role,
)
from utils import html_text, format_local, format_local_date, now_local, display_name, LOCAL_TZ
import jalali

logger = logging.getLogger(__name__)

# Eighth-blocks, low to high. Telegram renders these in-line without any
# image generation, which keeps the bot dependency-free.
_SPARK = "▁▂▃▄▅▆▇█"


def _fa(value) -> str:
    """Persian-Indic digits for a number."""
    return jalali.to_persian_digits(value)


def sparkline(values) -> str:
    """Render a series as block characters.

    Scales against the maximum so the shape is readable regardless of volume.
    An all-zero series renders as flat baseline rather than a misleading spike.
    """
    values = list(values)
    if not values:
        return ""
    peak = max(values)
    if peak <= 0:
        return _SPARK[0] * len(values)
    out = []
    for v in values:
        # Any non-zero value gets at least the second level, so "1 post" is
        # visually distinct from "no posts".
        if v <= 0:
            out.append(_SPARK[0])
        else:
            idx = 1 + round((v / peak) * (len(_SPARK) - 2))
            out.append(_SPARK[min(idx, len(_SPARK) - 1)])
    return "".join(out)


def bar(value: int, total: int, width: int = 10) -> str:
    """A fixed-width proportion bar.

    The last block is reserved until the ratio is genuinely 100%, so 96% does
    not render as a full bar and read as "perfect".
    """
    if not total:
        return "░" * width
    ratio = value / total
    if ratio >= 1:
        return "█" * width
    filled = min(int(ratio * width), width - 1)
    return "█" * filled + "░" * (width - filled)


def pct(part, whole) -> str:
    """Percentage as Persian digits, or an em dash when undefined."""
    part = int(part or 0)
    whole = int(whole or 0)
    if not whole:
        return "—"
    return f"{_fa(round(part * 100 / whole))}٪"


def _delta(current: int, previous: int) -> str:
    """'(+۳)' / '(−۲)' / '' comparing a period with the one before it."""
    diff = int(current or 0) - int(previous or 0)
    if diff > 0:
        return f" (+{_fa(diff)})"
    if diff < 0:
        return f" (−{_fa(abs(diff))})"
    return ""


def _humanise_seconds(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if abs(seconds) < 60:
        return f"{_fa(seconds)} ثانیه"
    minutes = seconds // 60
    if abs(minutes) < 60:
        return f"{_fa(minutes)} دقیقه"
    return f"{_fa(round(minutes / 60, 1))} ساعت"


# Telegram rejects messages over 4096 characters. Screens that grow with the
# number of channels or authors must be clipped rather than raising BadRequest.
TELEGRAM_LIMIT = 4096


def clip(text: str, limit: int = TELEGRAM_LIMIT) -> str:
    """Trim to the last whole line that fits, leaving room for a notice."""
    if len(text) <= limit:
        return text
    notice = "\n\n<i>… فهرست طولانی بود و کوتاه شد.</i>"
    budget = limit - len(notice)
    cut = text[:budget]
    if "\n" in cut:
        cut = cut[:cut.rindex("\n")]
    return cut + notice


_TYPE_LABELS = {
    "text": "📝 متن", "photo": "🖼️ عکس", "video": "🎬 ویدیو",
    "document": "📎 فایل", "media_group": "📦 آلبوم",
}


def stats_menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 خلاصه", callback_data="stats_summary"),
         InlineKeyboardButton("📈 روند", callback_data="stats_trend")],
        [InlineKeyboardButton("🕒 زمان‌بندی", callback_data="stats_schedule")],
    ]
    if is_admin:
        rows.insert(1, [InlineKeyboardButton("📣 کانال‌ها", callback_data="stats_channels"),
                        InlineKeyboardButton("👤 نویسندگان", callback_data="stats_authors")])
    rows.append([InlineKeyboardButton("◀️ ابزارها", callback_data="tools_menu")])
    return InlineKeyboardMarkup(rows)


def _back_keyboard(is_admin: bool, refresh: str = "stats_summary") -> InlineKeyboardMarkup:
    """Footer for a stats screen.

    ``refresh`` must be the callback of the screen being rendered, otherwise
    the button silently navigates the user to the summary instead of
    reloading what they are looking at.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 بروزرسانی", callback_data=refresh)],
        [InlineKeyboardButton("◀️ آمار", callback_data="tools_stats")],
    ])


async def _scope_for(user_id: int):
    """(user_id_filter, is_admin). ``None`` filter means 'everything'."""
    role = await get_user_role(user_id)
    is_admin = role in ("sudo", "owner")
    return (None if is_admin else user_id), is_admin


async def _render(update, text: str, markup):
    text = clip(text)
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest as exc:
            # "message is not modified" fires when refreshing unchanged stats.
            if "not modified" not in str(exc).lower():
                await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def _guard(update) -> int | None:
    """Return the acting user id, or None after replying with a refusal."""
    user = update.effective_user
    if user is None:
        return None
    if not await is_writer_or_above(user.id):
        if update.callback_query:
            await update.callback_query.answer("❌ غیرمجاز.", show_alert=True)
        else:
            await update.message.reply_text("❌ غیرمجاز.")
        return None
    return user.id


# --- Summary -----------------------------------------------------------------

def summary_text(data: dict, is_admin: bool) -> str:
    posts = data.get("posts") or {}
    windows = data.get("windows") or {}
    deliveries = data.get("deliveries") or {}
    total = int(posts.get("total") or 0)

    lines = ["📊 <b>خلاصه عملکرد</b>", ""]
    if not is_admin:
        lines.append("<i>فقط پست‌های شما</i>")
        lines.append("")

    lines.append(
        f"🕐 امروز: <b>{_fa(windows.get('last_24h', 0))}</b>"
        f"{_delta(windows.get('last_24h', 0), windows.get('prev_24h', 0))}"
    )
    lines.append(
        f"📅 ۷ روز اخیر: <b>{_fa(windows.get('last_7d', 0))}</b>"
        f"{_delta(windows.get('last_7d', 0), windows.get('prev_7d', 0))}"
    )
    lines.append(f"🗓 ۳۰ روز اخیر: <b>{_fa(windows.get('last_30d', 0))}</b>")
    lines.append(f"📦 مجموع: <b>{_fa(total)}</b>")
    lines.append("")

    # Delivery quality is the number that actually matters, and it comes from
    # per-channel rows rather than the coarse post-level status.
    attempted = int(deliveries.get("attempted") or 0)
    delivered = int(deliveries.get("delivered") or 0)
    tracked_posts = int(deliveries.get("tracked_posts") or 0)
    if attempted:
        lines.append("🎯 <b>کیفیت ارسال</b>")
        lines.append(f"{bar(delivered, attempted)} {pct(delivered, attempted)}")
        lines.append(f"موفق: {_fa(delivered)} از {_fa(attempted)} ارسال"
                     f" — در {_fa(tracked_posts)} پست")
        # Per-destination tracking only began with the retry system, so this
        # covers fewer posts than the status counts below. Saying so prevents
        # a confusing "100%" sitting directly above "۴ ناقص".
        untracked = total - tracked_posts
        if untracked > 0:
            lines.append(f"<i>ثبت جزئیات ارسال از زمان فعال‌سازی این قابلیت است؛"
                         f" {_fa(untracked)} پست قدیمی‌تر در این نسبت نیستند.</i>")
    else:
        lines.append("🎯 <b>کیفیت ارسال</b>")
        lines.append("<i>هنوز ارسالی با جزئیات ثبت نشده است.</i>")
    lines.append("")

    lines.append("📋 <b>وضعیت پست‌ها</b>")
    lines.append(f"✅ کامل: {_fa(posts.get('completed') or 0)}   "
                 f"⚠️ ناقص: {_fa(posts.get('partial') or 0)}   "
                 f"❌ ناموفق: {_fa(posts.get('failed') or 0)}")
    lines.append(f"💾 پیش‌نویس: {_fa(posts.get('drafts') or 0)}   "
                 f"🕒 زمان‌بندی: {_fa(posts.get('scheduled') or 0)}   "
                 f"🔐 تأیید: {_fa(posts.get('approvals') or 0)}")

    types = data.get("types") or []
    if types:
        lines.append("")
        lines.append("🗂 <b>نوع محتوا</b>")
        shown = types[:4]
        for row in shown:
            label = _TYPE_LABELS.get(row["post_type"], row["post_type"])
            lines.append(f"{label}: {_fa(row['total'])} ({pct(row['total'], total)})")

    # Anything that needs a human is collected at the end, so the screen has a
    # clear "do something" section instead of burying it among counters.
    alerts = []
    retries = int(data.get("pending_retries") or 0)
    if retries:
        alerts.append(f"🔁 {_fa(retries)} ارسال در صف تلاش مجدد")
    if int(posts.get("approvals") or 0):
        alerts.append(f"🔐 {_fa(posts['approvals'])} پست در انتظار تأیید")
    failing = int(deliveries.get("failing") or 0)
    if failing:
        alerts.append(f"❌ {_fa(failing)} مقصد ناموفق")
    if alerts:
        lines.append("")
        lines.append("⚡ <b>نیازمند توجه</b>")
        lines.extend(f"• {a}" for a in alerts)

    if is_admin:
        channels = data.get("channels") or {}
        platforms = data.get("platforms") or []
        if channels:
            lines.append("")
            plat = " | ".join(f"{p['platform']}: {_fa(p['total'])}" for p in platforms) or "—"
            lines.append(f"📣 کانال‌ها: {_fa(channels.get('active') or 0)}/"
                         f"{_fa(channels.get('total') or 0)} فعال — {plat}")

    return "\n".join(lines)


async def handle_stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /stats and the tools button."""
    from handlers.post import cancel_all_workflows

    user_id = await _guard(update)
    if user_id is None:
        return
    cancel_all_workflows(user_id)
    if update.callback_query:
        await update.callback_query.answer()

    scope, is_admin = await _scope_for(user_id)
    today = format_local_date(now_local().date(), long_form=True)
    text = (
        "📊 <b>آمار و گزارش</b>\n\n"
        f"امروز: {html_text(today)}\n\n"
        "یک بخش را انتخاب کنید:"
    )
    if not is_admin:
        text += "\n\n<i>شما آمار پست‌های خودتان را می‌بینید.</i>"
    await _render(update, text, stats_menu_keyboard(is_admin))


async def handle_stats_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = await _guard(update)
    if user_id is None:
        return
    if update.callback_query:
        await update.callback_query.answer()
    scope, is_admin = await _scope_for(user_id)
    data = await get_analytics(scope)
    await _render(update, summary_text(data, is_admin), _back_keyboard(is_admin, "stats_summary"))


# --- Trend -------------------------------------------------------------------

def trend_text(daily, hourly, is_admin: bool) -> str:
    lines = ["📈 <b>روند فعالیت</b>", ""]
    if not is_admin:
        lines.append("<i>فقط پست‌های شما</i>")
        lines.append("")

    totals = [d["total"] for d in daily]
    if not any(totals):
        lines.append("در این بازه پستی ثبت نشده است.")
        return "\n".join(lines)

    lines.append(f"<b>۱۴ روز اخیر</b> (مجموع {_fa(sum(totals))})")
    lines.append(f"<code>{sparkline(totals)}</code>")
    first, last = daily[0]["day"], daily[-1]["day"]
    lines.append(f"<i>{html_text(format_local_date(first))} → "
                 f"{html_text(format_local_date(last))}</i>")
    lines.append("")

    busiest = max(daily, key=lambda d: d["total"])
    if busiest["total"]:
        lines.append(f"🔝 شلوغ‌ترین روز: {html_text(format_local_date(busiest['day'], long_form=True))}"
                     f" — {_fa(busiest['total'])} پست")
    quiet = sum(1 for d in daily if d["total"] == 0)
    if quiet:
        lines.append(f"😴 روزهای بدون پست: {_fa(quiet)} از {_fa(len(daily))}")
    lines.append(f"📊 میانگین روزانه: {_fa(round(sum(totals) / len(totals), 1))}")
    lines.append("")

    # Last seven days broken out, newest first — the sparkline shows shape,
    # this shows the actual numbers.
    lines.append("<b>۷ روز اخیر</b>")
    # Bars are scaled to the busiest of these seven days so they compare
    # volume. Encoding success ratio here made a 15-post day and a 6-post day
    # look identical.
    week = daily[-7:]
    week_peak = max((d["total"] for d in week), default=0) or 1
    for day in reversed(week):
        label = format_local_date(day["day"])
        mark = bar(day["total"], week_peak, width=8)
        missed = day["total"] - day["completed"]
        suffix = f" ⚠️{_fa(missed)}" if missed > 0 else ""
        lines.append(f"<code>{mark}</code> {html_text(label)} — {_fa(day['total'])}{suffix}")

    if any(hourly):
        lines.append("")
        peak_hour = hourly.index(max(hourly))
        tzname = LOCAL_TZ.key if hasattr(LOCAL_TZ, "key") else str(LOCAL_TZ)
        lines.append(f"<b>ساعات فعالیت</b> (۳۰ روز، به وقت {html_text(tzname)})")
        lines.append(f"<code>{sparkline(hourly)}</code>")
        # An arrow axis renders unpredictably inside an RTL message, so the
        # endpoints are labelled explicitly instead.
        lines.append(f"<i>از ساعت ۰۰ (چپ) تا ۲۳ (راست)</i>")
        lines.append(f"⏰ پرکارترین ساعت: {_fa(f'{peak_hour:02d}')}:۰۰")

    return "\n".join(lines)


async def handle_stats_trend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = await _guard(update)
    if user_id is None:
        return
    if update.callback_query:
        await update.callback_query.answer()
    scope, is_admin = await _scope_for(user_id)
    daily = await get_daily_counts(14, scope)
    hourly = await get_hourly_distribution(30, scope)
    await _render(update, trend_text(daily, hourly, is_admin), _back_keyboard(is_admin, "stats_trend"))


# --- Channels (admin only) ---------------------------------------------------

def channels_text(rows, failures, health) -> str:
    lines = ["📣 <b>عملکرد کانال‌ها</b> <i>(۳۰ روز)</i>", ""]
    if not rows:
        lines.append("هنوز ارسالی ثبت نشده است.")
        return "\n".join(lines)

    # The per-row lines spell out "N موفق از M ارسال", so the figures are
    # self-explanatory without a legend header.
    health_by_id = {h["id"]: h for h in health}
    # Worst-first ordering from the query means the interesting rows come
    # first; cap the rest so the message stays inside Telegram's limit.
    shown, hidden = rows[:12], max(0, len(rows) - 12)
    for row in shown:
        attempted = int(row["attempted"] or 0)
        delivered = int(row["delivered"] or 0)
        failing = int(row["failing"] or 0)
        icon = "🔵" if row["platform"] == "bale" else "📣"
        if failing == 0:
            status = "✅"
        elif delivered == 0:
            status = "🔴"
        else:
            status = "⚠️"
        if row.get("is_active") is not None and not row["is_active"]:
            status += "⏸"

        lines.append(f"{status} {icon} <b>{html_text(row['name'])}</b>")
        lines.append(f"   <code>{bar(delivered, attempted)}</code> {pct(delivered, attempted)} موفق")
        lines.append(f"   ✅ {_fa(delivered)} موفق از {_fa(attempted)} ارسال"
                     + (f" · ❌ {_fa(failing)} ناموفق" if failing else ""))

        # Retries per delivery exposes a channel that "works" but only after
        # repeated attempts, which a success rate alone would hide.
        total_attempts = int(row.get("total_attempts") or 0)
        if attempted and total_attempts > attempted:
            ratio = round(total_attempts / attempted, 1)
            lines.append(f"   🔁 میانگین تلاش: {_fa(ratio)}")
        if failing and row.get("last_error"):
            lines.append(f"   ❌ {html_text(str(row['last_error'])[:90])}")
        hp = health_by_id.get(row["channel_id"])
        if hp and hp.get("last_health_status") == "unhealthy":
            lines.append("   🩺 بررسی سلامت: ناموفق")
        elif hp and hp.get("last_health_status") == "degraded":
            lines.append("   🩺 بررسی سلامت: برخی ربات‌ها به این کانال دسترسی ندارند")
        lines.append("")

    if hidden:
        lines.append(f"<i>و {_fa(hidden)} کانال دیگر…</i>")
        lines.append("")

    if failures:
        lines.append("🔎 <b>شایع‌ترین خطاها</b>")
        for f in failures:
            lines.append(f"• {_fa(f['total'])}× {html_text(str(f['reason'])[:90])}")

    return "\n".join(lines)


async def handle_stats_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = await _guard(update)
    if user_id is None:
        return
    if not await is_owner(user_id):
        await update.callback_query.answer("❌ فقط مالک یا sudo.", show_alert=True)
        return
    await update.callback_query.answer()
    rows = await get_channel_stats(30)
    failures = await get_failure_breakdown(30)
    health = await get_channel_health()
    await _render(update, channels_text(rows, failures, health), _back_keyboard(True, "stats_channels"))


# --- Authors (admin only) ----------------------------------------------------

_ROLE_LABELS = {"sudo": "👑", "owner": "⭐", "writer": "✏️"}


def authors_text(rows) -> str:
    lines = ["👤 <b>نویسندگان</b> <i>(۳۰ روز)</i>", ""]
    if not rows:
        lines.append("در این بازه پستی ثبت نشده است.")
        return "\n".join(lines)

    top = int(rows[0]["total"] or 0)
    for i, row in enumerate(rows, 1):
        total = int(row["total"] or 0)
        completed = int(row["completed"] or 0)
        role = _ROLE_LABELS.get(row.get("role"), "•")
        lines.append(f"{_fa(i)}. {role} <b>{html_text(display_name(row))}</b>")
        lines.append(f"   <code>{bar(total, top)}</code> {_fa(total)} پست"
                     f" — موفق {pct(completed, total)}")
        problems = int(row.get("problems") or 0)
        if problems:
            lines.append(f"   ⚠️ {_fa(problems)} پست ناقص/ناموفق")
        if row.get("last_post"):
            lines.append(f"   🕐 آخرین: {html_text(format_local(row['last_post']))}")
        lines.append("")
    return "\n".join(lines)


async def handle_stats_authors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = await _guard(update)
    if user_id is None:
        return
    if not await is_owner(user_id):
        await update.callback_query.answer("❌ فقط مالک یا sudo.", show_alert=True)
        return
    await update.callback_query.answer()
    rows = await get_author_stats(30)
    await _render(update, authors_text(rows), _back_keyboard(True, "stats_authors"))


# --- Scheduling --------------------------------------------------------------

def schedule_text(stats: dict, is_admin: bool) -> str:
    lines = ["🕒 <b>آمار زمان‌بندی</b> <i>(۳۰ روز)</i>", ""]
    if not is_admin:
        lines.append("<i>فقط زمان‌بندی‌های شما</i>")
        lines.append("")

    total = int(stats.get("total") or 0)
    if not total:
        lines.append("در این بازه زمان‌بندی‌ای ثبت نشده است.")
    else:
        completed = int(stats.get("completed") or 0)
        lines.append(f"<code>{bar(completed, total)}</code> {pct(completed, total)} موفق")
        lines.append(f"✅ انجام شد: {_fa(completed)} از {_fa(total)}")
        for key, label, icon in (
            ("failed", "ناموفق", "❌"),
            ("expired", "منقضی (ربات خاموش بوده)", "⏰"),
            ("cancelled", "لغو شده", "🚫"),
        ):
            value = int(stats.get(key) or 0)
            if value:
                lines.append(f"{icon} {label}: {_fa(value)}")

        delay = stats.get("avg_delay_seconds")
        if delay is not None:
            lines.append("")
            # The worker ticks every 60s, so a small positive delay is normal
            # and worth explaining rather than looking like a fault.
            lines.append(f"⏱ میانگین تأخیر اجرا: {_humanise_seconds(delay)}")
            if delay <= 90:
                lines.append("<i>(بررسی هر ۶۰ ثانیه انجام می‌شود، این مقدار طبیعی است)</i>")

    pending = int(stats.get("pending") or 0)
    lines.append("")
    lines.append(f"📋 در صف: <b>{_fa(pending)}</b>")
    upcoming = stats.get("upcoming") or []
    if upcoming:
        lines.append("")
        lines.append("<b>زمان‌بندی‌های بعدی</b>")
        for row in upcoming:
            lines.append(f"• #{_fa(row['post_id'])} — {html_text(format_local(row['run_at']))}")
    return "\n".join(lines)


async def handle_stats_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = await _guard(update)
    if user_id is None:
        return
    if update.callback_query:
        await update.callback_query.answer()
    scope, is_admin = await _scope_for(user_id)
    stats = await get_schedule_stats(30, scope)
    await _render(update, schedule_text(stats, is_admin), _back_keyboard(is_admin, "stats_schedule"))


# --- Daily digest ------------------------------------------------------------

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    """Owner digest: what actually happened today, not an all-time dump."""
    from config import SUDO_USER_ID
    try:
        data = await get_analytics(None)
        daily = await get_daily_counts(7, None)
        schedule = await get_schedule_stats(1, None)
        channels = await get_channel_stats(1)

        windows = data.get("windows") or {}
        today = int(windows.get("last_24h") or 0)
        yesterday = int(windows.get("prev_24h") or 0)

        lines = [
            f"🌙 <b>گزارش روزانه</b> — {html_text(format_local_date(now_local().date(), long_form=True))}",
            "",
            f"📝 پست‌های امروز: <b>{_fa(today)}</b>{_delta(today, yesterday)}",
            f"<code>{sparkline([d['total'] for d in daily])}</code> <i>۷ روز اخیر</i>",
        ]

        troubled = [c for c in channels if int(c.get("failing") or 0)]
        if troubled:
            lines.append("")
            lines.append("⚠️ <b>کانال‌های مشکل‌دار امروز</b>")
            for c in troubled[:5]:
                lines.append(f"• {html_text(c['name'])}: {_fa(c['failing'])} ناموفق")

        retries = int(data.get("pending_retries") or 0)
        approvals = int((data.get("posts") or {}).get("approvals") or 0)
        pending = int(schedule.get("pending") or 0)
        if retries or approvals or pending:
            lines.append("")
            lines.append("📌 <b>در انتظار</b>")
            if retries:
                lines.append(f"• 🔁 {_fa(retries)} تلاش مجدد")
            if approvals:
                lines.append(f"• 🔐 {_fa(approvals)} تأیید")
            if pending:
                lines.append(f"• 🕒 {_fa(pending)} زمان‌بندی")

        expired = int(schedule.get("expired") or 0)
        if expired:
            lines.append("")
            lines.append(f"⏰ {_fa(expired)} زمان‌بندی امروز از دست رفت.")

        if not today and not troubled and not retries:
            lines.append("")
            lines.append("✨ امروز بدون فعالیت و بدون خطا.")

        await context.bot.send_message(
            chat_id=SUDO_USER_ID, text="\n".join(lines), parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Could not send daily report")
