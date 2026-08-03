import logging
import time

from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from database import (
    is_sudo, is_owner, add_user, remove_user, get_all_users,
    get_user_role, update_user_role, get_user,
)
from keyboards import users_menu_keyboard, users_list_keyboard, user_detail_keyboard, role_select_keyboard, main_menu_keyboard
from utils import state_is_expired, display_name, html_text

logger = logging.getLogger(__name__)

_add_user_states: dict[int, dict] = {}


async def _safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except BadRequest:
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


async def handle_users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not await is_owner(user_id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    await _safe_edit(query, "👥 <b>مدیریت کاربران</b>", reply_markup=users_menu_keyboard())


async def handle_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not await is_owner(user_id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    await _safe_edit(query, "➕ <b>افزودن کاربر</b>\n\nشناسه تلگرام کاربر را ارسال کنید:")
    _add_user_states[user_id] = {"step": "waiting_id", "created_at": time.monotonic()}


async def handle_add_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = _add_user_states.get(user_id)
    if state and state_is_expired(state):
        _add_user_states.pop(user_id, None)
        state = None
    if not state:
        return False

    if not update.message or not update.message.text:
        return False

    text = update.message.text.strip()

    if state["step"] == "waiting_id":
        try:
            target_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ شناسه نامعتبر است. یک عدد ارسال کنید.")
            return True

        # Owner can only add writers
        role = await get_user_role(user_id)
        if role == "owner":
            result = await add_user(target_id, "writer", added_by=user_id)
            if result:
                await update.message.reply_text(
                    f"✅ کاربر <code>{target_id}</code> به عنوان نویسنده اضافه شد.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_menu_keyboard(is_owner=True),
                )
            else:
                await update.message.reply_text(
                    f"⚠️ کاربر <code>{target_id}</code> قبلاً exists.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_menu_keyboard(is_owner=True),
                )
            _add_user_states.pop(user_id, None)
            return True

        # Sudo can choose role
        _add_user_states[user_id] = {"step": "waiting_role", "target_id": target_id, "created_at": time.monotonic()}
        await update.message.reply_text(
            f"نقش کاربر <code>{target_id}</code> را انتخاب کنید:",
            parse_mode=ParseMode.HTML,
            reply_markup=role_select_keyboard(),
        )
        return True

    return False


async def handle_role_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    # Granting a role is the highest-value action in the bot; re-check ownership
    # here rather than trusting that the state could only have been armed by one.
    if not await is_owner(user_id):
        _add_user_states.pop(user_id, None)
        await _safe_edit(query, "❌ غیرمجاز.")
        return
    state = _add_user_states.get(user_id)
    if state and state_is_expired(state):
        _add_user_states.pop(user_id, None)
        state = None
    if not state or state.get("step") != "waiting_role":
        return

    target_id = state["target_id"]
    role = query.data.removeprefix("role_")
    _add_user_states.pop(user_id, None)

    result = await add_user(target_id, role, added_by=user_id)
    if result:
        role_label = "مالک" if role == "owner" else "نویسنده"
        await _safe_edit(
            query,
            f"✅ کاربر <code>{target_id}</code> به عنوان {role_label} اضافه شد.",
            reply_markup=main_menu_keyboard(is_sudo=True),
        )
    else:
        await _safe_edit(
            query,
            f"⚠️ کاربر <code>{target_id}</code> قبلاً وجود دارد.",
            reply_markup=main_menu_keyboard(is_sudo=True),
        )


async def handle_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not await is_owner(user_id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    users = await get_all_users()
    if not users:
        await _safe_edit(query, "📋 هیچ کاربری وجود ندارد.", reply_markup=users_menu_keyboard())
        return

    text = "📋 <b>لیست کاربران:</b>\n\n"
    role_labels = {"sudo": "👑 ادمین اصلی", "owner": "⭐ مالک", "writer": "✏️ نویسنده"}
    for u in users:
        role_label = role_labels.get(u["role"], u["role"])
        label = display_name(u)
        # When the bot has never seen the user act, display_name() falls back
        # to "#id"; printing the id again underneath would just repeat it.
        if label == f"#{u['user_id']}":
            text += f"• <code>{u['user_id']}</code> ({role_label})\n"
            text += "  <i>هنوز نامی ثبت نشده</i>\n"
        else:
            text += f"• {html_text(label)} ({role_label})\n"
            text += f"  <code>{u['user_id']}</code>\n"

    await _safe_edit(query, text, reply_markup=users_list_keyboard(users))


async def handle_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not await is_owner(user_id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    try:
        target_id = int(query.data.removeprefix("user_info_"))
    except (IndexError, ValueError):
        return

    role = await get_user_role(target_id)
    if not role:
        await _safe_edit(query, "❌ کاربر یافت نشد.", reply_markup=users_menu_keyboard())
        return

    role_labels = {"sudo": "👑 ادمین اصلی", "owner": "⭐ مالک", "writer": "✏️ نویسنده"}
    role_label = role_labels.get(role, role)

    profile = await get_user(target_id)
    text = f"<b>اطلاعات کاربر:</b>\n\n"
    text += f"👤 نام: {html_text(display_name(profile or {'user_id': target_id}))}\n"
    text += f"شناسه: <code>{target_id}</code>\n"
    text += f"نقش: {role_label}\n"

    # Owner can't manage sudo or other owners
    caller_role = await get_user_role(user_id)
    if caller_role == "owner" and role in ("sudo", "owner"):
        await _safe_edit(query, text, reply_markup=users_list_keyboard(await get_all_users()))
        return

    await _safe_edit(query, text, reply_markup=user_detail_keyboard(target_id, role))


async def handle_promote_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not await is_sudo(user_id):
        await query.answer("❌ فقط sudo می‌تواند نویسنده را به owner ارتقا دهد.", show_alert=True)
        return

    try:
        target_id = int(query.data.removeprefix("promote_owner_"))
    except (IndexError, ValueError):
        return

    await update_user_role(target_id, "owner")
    await _safe_edit(query, f"✅ کاربر <code>{target_id}</code> به مالک ارتقا یافت.", reply_markup=users_menu_keyboard())


async def handle_demote_writer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not await is_owner(user_id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    try:
        target_id = int(query.data.removeprefix("demote_writer_"))
    except (IndexError, ValueError):
        return

    await update_user_role(target_id, "writer")
    await _safe_edit(query, f"✅ کاربر <code>{target_id}</code> به نویسنده تنزل یافت.", reply_markup=users_menu_keyboard())


async def handle_remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not await is_owner(user_id):
        await query.edit_message_text("❌ غیرمجاز.")
        return

    try:
        target_id = int(query.data.removeprefix("remove_user_"))
    except (IndexError, ValueError):
        return

    role = await get_user_role(target_id)
    if role == "sudo":
        await _safe_edit(query, "❌ نمی‌توان ادمین اصلی را حذف کرد.", reply_markup=users_menu_keyboard())
        return

    # Owner can only remove writers
    caller_role = await get_user_role(user_id)
    if caller_role == "owner" and role == "owner":
        await _safe_edit(query, "❌ مالک نمی‌تواند مالک دیگری را حذف کند.", reply_markup=users_menu_keyboard())
        return

    await remove_user(target_id)
    await _safe_edit(query, f"✅ کاربر <code>{target_id}</code> حذف شد.", reply_markup=users_menu_keyboard())
