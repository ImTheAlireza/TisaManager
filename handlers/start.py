from telegram import Update
from telegram.ext import ContextTypes

from database import is_sudo, is_owner, is_writer_or_above
from keyboards import main_menu_keyboard


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not await is_writer_or_above(user_id):
        await update.message.reply_text("❌ شما مجاز به استفاده از این ربات نیستید.")
        return

    sudo = await is_sudo(user_id)
    owner = await is_owner(user_id)

    await update.message.reply_text(
        "🤖 ربات مدیریت پست\n\nیک گزینه را انتخاب کنید:",
        reply_markup=main_menu_keyboard(is_sudo=sudo, is_owner=owner),
    )
