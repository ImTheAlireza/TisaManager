from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard(is_sudo: bool = False, is_owner: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📝 پست جدید", callback_data="new_post")],
        [InlineKeyboardButton("📋 تاریخچه پست‌ها", callback_data="history")],
        [InlineKeyboardButton("🕒 پست‌های زمان‌بندی‌شده", callback_data="scheduled_list")],
        [InlineKeyboardButton("🧰 ابزارها", callback_data="tools_menu")],
        [InlineKeyboardButton("❓ راهنمای استفاده", callback_data="help")],
    ]
    if is_sudo or is_owner:
        buttons.append([InlineKeyboardButton("👥 مدیریت کاربران", callback_data="users_menu")])
    if is_sudo or is_owner:
        buttons.append([InlineKeyboardButton("⚙️ تنظیمات", callback_data="settings")])
    return InlineKeyboardMarkup(buttons)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ تأیید و ارسال", callback_data="confirm_post")],
            [InlineKeyboardButton("🎯 انتخاب کانال‌ها", callback_data="choose_channels")],
            [
                InlineKeyboardButton("💾 ذخیره پیش‌نویس", callback_data="save_draft"),
            ],
            [InlineKeyboardButton("🕒 زمان‌بندی", callback_data="schedule_post"),
            ],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_post")],
        ]
    )


def channel_selection_keyboard(channels: list[dict], selected: set[int]) -> InlineKeyboardMarkup:
    buttons = []
    for channel in channels:
        marker = "✅" if channel["id"] in selected else "⬜"
        icon = "🔵" if channel.get("platform") == "bale" else "📣"
        buttons.append([InlineKeyboardButton(
            f"{marker} {icon} {channel['name']}",
            callback_data=f"toggle_channel_{channel['id']}",
        )])
    buttons.append([InlineKeyboardButton("✅ تأیید انتخاب", callback_data="channels_done")])
    buttons.append([InlineKeyboardButton("◀️ بازگشت", callback_data="channels_back")])
    return InlineKeyboardMarkup(buttons)


def schedule_date_keyboard(today) -> InlineKeyboardMarkup:
    """Seven selectable days starting today, labelled on the Persian calendar.

    The callback payload stays an ISO *Gregorian* date, so storage and routing
    are unaffected by the display calendar — only the label is localised.
    """
    from datetime import timedelta
    import jalali
    from utils import USE_JALALI

    labels = {0: "امروز", 1: "فردا", 2: "پس‌فردا"}
    rows, row = [], []
    for offset in range(7):
        day = today + timedelta(days=offset)
        if USE_JALALI:
            jy, jm, jd = jalali.to_jalali(day)
            pretty = f"{jalali.to_persian_digits(jd)} {jalali.MONTH_NAMES[jm]}"
        else:
            pretty = f"{day:%m/%d}"
        label = labels.get(offset) or f"{jalali.weekday_name(day)} {pretty}"
        row.append(InlineKeyboardButton(label, callback_data=f"schedule_date_{day.isoformat()}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("📅 انتخاب از تقویم", callback_data="schedule_cal")])
    rows.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_post")])
    return InlineKeyboardMarkup(rows)


# Persian week starts on Saturday; date.weekday() has Monday == 0.
_SATURDAY_FIRST = (5, 6, 0, 1, 2, 3, 4)
_WEEK_HEADERS = ("ش", "ی", "د", "س", "چ", "پ", "ج")


def schedule_calendar_keyboard(jy: int, jm: int, today) -> InlineKeyboardMarkup:
    """A full Jalali month grid.

    Days before today are rendered as inert dots rather than tappable buttons,
    so a past date cannot be selected at all.
    """
    import jalali
    from datetime import date

    rows = [[InlineKeyboardButton(h, callback_data="schedule_noop") for h in _WEEK_HEADERS]]

    first = jalali.from_jalali(jy, jm, 1)
    # Column of the 1st, counting from Saturday.
    lead = _SATURDAY_FIRST.index(first.weekday())
    total = jalali.days_in_jalali_month(jy, jm)

    cells = [None] * lead
    for day in range(1, total + 1):
        cells.append(day)
    while len(cells) % 7:
        cells.append(None)

    for i in range(0, len(cells), 7):
        row = []
        for cell in cells[i:i + 7]:
            if cell is None:
                row.append(InlineKeyboardButton(" ", callback_data="schedule_noop"))
                continue
            g = jalali.from_jalali(jy, jm, cell)
            if g < today:
                row.append(InlineKeyboardButton("·", callback_data="schedule_noop"))
            else:
                mark = "🔸" if g == today else ""
                row.append(InlineKeyboardButton(
                    f"{mark}{jalali.to_persian_digits(cell)}",
                    callback_data=f"schedule_date_{g.isoformat()}",
                ))
        rows.append(row)

    prev_y, prev_m = (jy, jm - 1) if jm > 1 else (jy - 1, 12)
    next_y, next_m = (jy, jm + 1) if jm < 12 else (jy + 1, 1)
    # Never offer a month that is entirely in the past.
    last_of_prev = jalali.from_jalali(prev_y, prev_m, jalali.days_in_jalali_month(prev_y, prev_m))
    nav = []
    if last_of_prev >= today:
        nav.append(InlineKeyboardButton("‹ ماه قبل", callback_data=f"schedule_cal_{prev_y}_{prev_m}"))
    nav.append(InlineKeyboardButton(
        f"{jalali.MONTH_NAMES[jm]} {jalali.to_persian_digits(jy)}",
        callback_data="schedule_noop",
    ))
    nav.append(InlineKeyboardButton("ماه بعد ›", callback_data=f"schedule_cal_{next_y}_{next_m}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("◀️ روزهای نزدیک", callback_data="schedule_back_date")])
    rows.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_post")])
    return InlineKeyboardMarkup(rows)


def schedule_hour_keyboard(date_iso: str, min_hour: int = 0) -> InlineKeyboardMarkup:
    """Hours for the chosen day, hiding hours that have already passed today."""
    import jalali
    from utils import USE_JALALI

    def _n(v):
        return jalali.to_persian_digits(f"{v:02d}") if USE_JALALI else f"{v:02d}"

    rows, row = [], []
    for hour in range(min_hour, 24):
        row.append(InlineKeyboardButton(_n(hour), callback_data=f"schedule_hour_{date_iso}_{hour}"))
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not rows:
        rows.append([InlineKeyboardButton("امروز ساعتی باقی نمانده", callback_data="schedule_noop")])
    rows.append([InlineKeyboardButton("◀️ تغییر تاریخ", callback_data="schedule_back_date")])
    rows.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_post")])
    return InlineKeyboardMarkup(rows)


def schedule_minute_keyboard(date_iso: str, hour: int, allowed_minutes=None) -> InlineKeyboardMarkup:
    """Five-minute granularity, restricted to minutes still in the future."""
    import jalali
    from utils import USE_JALALI

    minutes = allowed_minutes if allowed_minutes is not None else range(0, 60, 5)
    rows, row = [], []
    for minute in minutes:
        label = f"{hour:02d}:{minute:02d}"
        row.append(InlineKeyboardButton(
            jalali.to_persian_digits(label) if USE_JALALI else label,
            callback_data=f"schedule_minute_{date_iso}_{hour}_{minute}",
        ))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not rows:
        rows.append([InlineKeyboardButton("دقیقه‌ای باقی نمانده", callback_data="schedule_noop")])
    rows.append([InlineKeyboardButton("◀️ تغییر ساعت", callback_data=f"schedule_back_hour_{date_iso}")])
    rows.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_post")])
    return InlineKeyboardMarkup(rows)


def scheduled_list_keyboard(schedules: list[dict], format_time) -> InlineKeyboardMarkup:
    """List of upcoming schedules; each row opens its detail view."""
    buttons = []
    for s in schedules:
        preview = (s.get("text") or s.get("caption") or "").strip().replace("\n", " ")
        if len(preview) > 24:
            preview = preview[:24] + "…"
        lock = "🔒 " if s.get("status") == "processing" else ""
        buttons.append([InlineKeyboardButton(
            f"{lock}🕒 {format_time(s['run_at'])} | {preview or '#' + str(s['post_id'])}",
            callback_data=f"sched_view_{s['id']}",
        )])
    if not buttons:
        buttons.append([InlineKeyboardButton("موردی وجود ندارد", callback_data="schedule_noop")])
    buttons.append([InlineKeyboardButton("◀️ بازگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


def scheduled_detail_keyboard(schedule_id: int, post_id: int, locked: bool = False) -> InlineKeyboardMarkup:
    """Detail view. A claimed ('processing') schedule offers no destructive action."""
    buttons = []
    if not locked:
        buttons.append([
            InlineKeyboardButton("🕒 تغییر زمان", callback_data=f"sched_time_{schedule_id}"),
            InlineKeyboardButton("🚫 لغو زمان‌بندی", callback_data=f"sched_cancel_{schedule_id}"),
        ])
        buttons.append([InlineKeyboardButton("⚡ انتشار فوری", callback_data=f"sched_now_{schedule_id}")])
    buttons.append([InlineKeyboardButton("📄 مشاهده پست", callback_data=f"post_{post_id}")])
    buttons.append([InlineKeyboardButton("◀️ بازگشت", callback_data="scheduled_list")])
    return InlineKeyboardMarkup(buttons)


def approval_settings_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    label = "🔴 خاموش کردن تأیید" if enabled else "🟢 روشن کردن تأیید"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="toggle_approval")],
        [InlineKeyboardButton("◀️ بازگشت به تنظیمات", callback_data="settings")],
    ])


def calendar_settings_keyboard(use_jalali: bool) -> InlineKeyboardMarkup:
    label = "🌍 نمایش میلادی" if use_jalali else "📅 نمایش شمسی"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="toggle_calendar")],
        [InlineKeyboardButton("◀️ بازگشت به تنظیمات", callback_data="settings")],
    ])


def restart_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ بله، ری‌استارت شود", callback_data="do_restart"),
                InlineKeyboardButton("❌ انصراف", callback_data="settings"),
            ]
        ]
    )


def settings_keyboard(is_sudo_user: bool = False) -> list[list[InlineKeyboardButton]]:
    buttons = [
        [InlineKeyboardButton("📢 مدیریت کانال‌ها", callback_data="manage_channels")],
        [InlineKeyboardButton("🔐 تنظیم تأیید پست‌ها", callback_data="approval_settings")],
        [InlineKeyboardButton("📅 تقویم نمایش تاریخ", callback_data="calendar_settings")],
    ]
    if is_sudo_user:
        buttons.append([InlineKeyboardButton("🗄️ پشتیبان‌گیری", callback_data="backup_project"), InlineKeyboardButton("♻️ بازیابی", callback_data="restore_project")])
        buttons.append([InlineKeyboardButton("🔄 ری‌استارت ربات", callback_data="bot_restart")])
    buttons.append([InlineKeyboardButton("◀️ بازگشت", callback_data="back_main")])
    return buttons


def settings_main_markup(is_sudo_user: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(settings_keyboard(is_sudo_user=is_sudo_user))


def channel_management_keyboard(channels: list[dict], is_sudo_user: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        icon = "🔵" if ch.get("platform") == "bale" else "📣"
        buttons.append([InlineKeyboardButton(
            f"🗑️ {icon} {ch['name']} ({ch['chat_type']})",
            callback_data=f"remove_{ch['id']}",
        )])
    buttons.extend([
        [InlineKeyboardButton("➕ افزودن کانال تلگرام", callback_data="add_channel")],
        [InlineKeyboardButton("➕ افزودن کانال بله", callback_data="add_bale_channel")],
        [InlineKeyboardButton("◀️ تنظیمات", callback_data="settings")],
    ])
    return InlineKeyboardMarkup(buttons)


def settings_markup(channels: list[dict], is_sudo_user: bool = False) -> InlineKeyboardMarkup:
    return channel_management_keyboard(channels, is_sudo_user=is_sudo_user)


def users_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ افزودن کاربر", callback_data="add_user")],
            [InlineKeyboardButton("📋 لیست کاربران", callback_data="list_users")],
            [InlineKeyboardButton("◀️ بازگشت", callback_data="back_main")],
        ]
    )


def users_list_keyboard(users: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    role_labels = {"sudo": "👑 ادمین اصلی", "owner": "⭐ مالک", "writer": "✏️ نویسنده"}
    for u in users:
        role_label = role_labels.get(u["role"], u["role"])
        name = u.get("name") or str(u["user_id"])
        buttons.append([
            InlineKeyboardButton(f"{name} ({role_label})", callback_data=f"user_info_{u['user_id']}"),
        ])
    buttons.append([InlineKeyboardButton("◀️ بازگشت", callback_data="users_menu")])
    return InlineKeyboardMarkup(buttons)


def user_detail_keyboard(user_id: int, role: str) -> InlineKeyboardMarkup:
    buttons = []
    if role == "writer":
        buttons.append([InlineKeyboardButton("⭐ ارتقا به مالک", callback_data=f"promote_owner_{user_id}")])
    if role == "owner":
        buttons.append([InlineKeyboardButton("✏️ تنزل به نویسنده", callback_data=f"demote_writer_{user_id}")])
    if role != "sudo":
        buttons.append([InlineKeyboardButton("❌ حذف کاربر", callback_data=f"remove_user_{user_id}")])
    buttons.append([InlineKeyboardButton("◀️ بازگشت", callback_data="list_users")])
    return InlineKeyboardMarkup(buttons)


def role_select_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⭐ مالک (Owner)", callback_data="role_owner")],
            [InlineKeyboardButton("✏️ نویسنده (Writer)", callback_data="role_writer")],
            [InlineKeyboardButton("❌ لغو", callback_data="users_menu")],
        ]
    )


def history_keyboard(posts: list[dict], page: int = 1, total_pages: int = 1, is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    for post in posts:
        type_icon = {"text": "📝", "photo": "🖼️", "video": "🎬", "document": "📎", "media_group": "📦"}.get(post["post_type"], "📝")
        preview = post.get("text") or post.get("caption") or ""
        if len(preview) > 30:
            preview = preview[:30] + "..."
        # Stored UTC -> display timezone. Rendering the raw value here showed
        # history entries in the DB server's zone instead of the user's.
        from utils import format_local_short
        date = format_local_short(post["created_at"])
        label = f"{type_icon} {date} | {preview}"
        if is_admin and post.get("user_id"):
            label = f"{type_icon} {date} | {preview}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"post_{post['id']}")])

    prev_page = max(1, page - 1)
    next_page = min(total_pages, page + 1)
    pag_row = [
        InlineKeyboardButton("<", callback_data=f"history_page_{prev_page}"),
        InlineKeyboardButton(f"{page}/{total_pages}", callback_data="history_noop"),
        InlineKeyboardButton(">", callback_data=f"history_page_{next_page}"),
    ]
    buttons.append(pag_row)
    buttons.append([InlineKeyboardButton("◀️ بازگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


def post_detail_keyboard(post_id: int, status: str = "completed", schedule_id: int = None) -> InlineKeyboardMarkup:
    if status == "draft":
        buttons = [[InlineKeyboardButton("✅ انتشار پیش‌نویس", callback_data=f"publish_draft_{post_id}")]]
    elif status == "scheduled":
        # A scheduled post must never offer "publish now" here: doing so would
        # send it once immediately and again when the job fires. Management
        # goes through the schedule itself.
        buttons = []
        if schedule_id:
            buttons.append([InlineKeyboardButton("🕒 مدیریت زمان‌بندی", callback_data=f"sched_view_{schedule_id}")])
        buttons.append([InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_{post_id}")])
    elif status == "pending_approval":
        buttons = [[InlineKeyboardButton("✅ تأیید انتشار", callback_data=f"approve_{post_id}")]]
    else:
        buttons = [
            [InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit_{post_id}"), InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_{post_id}")],
            [InlineKeyboardButton("📄 کپی", callback_data=f"duplicate_{post_id}"), InlineKeyboardButton("🔁 ارسال مجدد", callback_data=f"retry_{post_id}")],
        ]
    buttons.append([InlineKeyboardButton("◀️ بازگشت", callback_data="history")])
    return InlineKeyboardMarkup(buttons)
