from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard(is_sudo: bool = False, is_owner: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📝 پست جدید", callback_data="new_post")],
        [InlineKeyboardButton("📋 تاریخچه پست‌ها", callback_data="history")],
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
                InlineKeyboardButton("📑 ذخیره قالب", callback_data="save_template"),
            ],
            [InlineKeyboardButton("🕒 زمان‌بندی", callback_data="schedule_post"),
            ],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_post")],
        ]
    )


def template_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📑 ذخیره قالب", callback_data="save_template")],
        [InlineKeyboardButton("❌ لغو", callback_data="cancel_post")],
    ])


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


def schedule_date_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("امروز", callback_data="schedule_date_today"), InlineKeyboardButton("فردا", callback_data="schedule_date_tomorrow")],
        [InlineKeyboardButton("❌ لغو", callback_data="cancel_post")],
    ])


def schedule_hour_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for start in range(0, 24, 6):
        rows.append([InlineKeyboardButton(f"{hour:02d}:00", callback_data=f"schedule_hour_{hour}") for hour in range(start, min(start + 6, 24))])
    rows.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_post")])
    return InlineKeyboardMarkup(rows)


def schedule_minute_keyboard(hour: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{hour:02d}:{minute:02d}", callback_data=f"schedule_minute_{hour}_{minute}") for minute in (0, 15, 30, 45)],
        [InlineKeyboardButton("❌ لغو", callback_data="cancel_post")],
    ])


def approval_settings_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    label = "🔴 خاموش کردن تأیید" if enabled else "🟢 روشن کردن تأیید"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="toggle_approval")],
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
        [InlineKeyboardButton("📑 افزودن قالب", callback_data="add_template")],
        [InlineKeyboardButton("🔐 تنظیم تأیید پست‌ها", callback_data="approval_settings")],
    ]
    if is_sudo_user:
        buttons.append([InlineKeyboardButton("🗄️ پشتیبان‌گیری", callback_data="backup_project"), InlineKeyboardButton("♻️ بازیابی", callback_data="restore_project")])
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
        date = post["created_at"].strftime("%m/%d %H:%M") if post["created_at"] else ""
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


def post_detail_keyboard(post_id: int, status: str = "completed") -> InlineKeyboardMarkup:
    if status == "draft":
        buttons = [[InlineKeyboardButton("✅ انتشار پیش‌نویس", callback_data=f"publish_draft_{post_id}")]]
    elif status == "pending_approval":
        buttons = [[InlineKeyboardButton("✅ تأیید انتشار", callback_data=f"approve_{post_id}")]]
    else:
        buttons = [
            [InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit_{post_id}"), InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_{post_id}")],
            [InlineKeyboardButton("📄 کپی", callback_data=f"duplicate_{post_id}"), InlineKeyboardButton("🔁 ارسال مجدد", callback_data=f"retry_{post_id}")],
        ]
    buttons.append([InlineKeyboardButton("◀️ بازگشت", callback_data="history")])
    return InlineKeyboardMarkup(buttons)
