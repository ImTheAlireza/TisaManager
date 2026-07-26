from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard(is_sudo: bool = False, is_owner: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📝 پست جدید", callback_data="new_post")],
        [InlineKeyboardButton("📋 تاریخچه پست‌ها", callback_data="history")],
    ]
    if is_sudo or is_owner:
        buttons.append([InlineKeyboardButton("👥 مدیریت کاربران", callback_data="users_menu")])
    if is_sudo or is_owner:
        buttons.append([InlineKeyboardButton("⚙️ تنظیمات", callback_data="settings")])
    return InlineKeyboardMarkup(buttons)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ تأیید و ارسال", callback_data="confirm_post"),
                InlineKeyboardButton("❌ لغو", callback_data="cancel_post"),
            ]
        ]
    )


def settings_keyboard(channels: list[dict], is_sudo_user: bool = False) -> list[list[InlineKeyboardButton]]:
    buttons = []
    for ch in channels:
        icon = "🔵" if ch.get("platform") == "bale" else "📣"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"🗑️ {icon} {ch['name']} ({ch['chat_type']})",
                    callback_data=f"remove_{ch['id']}",
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton("➕ افزودن کانال تلگرام", callback_data="add_channel")]
    )
    buttons.append(
        [InlineKeyboardButton("➕ افزودن کانال بله", callback_data="add_bale_channel")]
    )
    if is_sudo_user:
        buttons.append(
            [
                InlineKeyboardButton("📊 وضعیت ربات", callback_data="bot_status"),
                InlineKeyboardButton("🔄 ری‌استارت", callback_data="bot_restart"),
            ]
        )
    buttons.append([InlineKeyboardButton("◀️ بازگشت", callback_data="back_main")])
    return buttons


def settings_markup(channels: list[dict], is_sudo_user: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(settings_keyboard(channels, is_sudo_user=is_sudo_user))


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


def post_detail_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit_{post_id}"),
                InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_{post_id}"),
            ],
            [InlineKeyboardButton("◀️ بازگشت", callback_data="history")],
        ]
    )
