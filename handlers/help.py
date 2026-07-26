from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database import is_writer_or_above, get_user_role
from keyboards import main_menu_keyboard


def _help_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 انتشار پست", callback_data="help_publish"), InlineKeyboardButton("👥 نقش‌ها", callback_data="help_roles")],
        [InlineKeyboardButton("⚙️ تنظیمات و کانال‌ها", callback_data="help_settings"), InlineKeyboardButton("📊 تاریخچه و ابزارها", callback_data="help_tools")],
        [InlineKeyboardButton("◀️ منوی اصلی", callback_data="back_main")],
    ])

TEXT = {
    "main": "<b>❓ راهنمای ربات مدیریت پست</b>\n\nیک بخش را انتخاب کنید:",
    "publish": "<b>📝 انتشار پست</b>\n\n1) روی «پست جدید» بزنید.\n2) متن، عکس، ویدیو، فایل یا آلبوم را بفرستید.\n3) پیش‌نمایش را بررسی کنید.\n4) در صورت نیاز کانال‌های مقصد را انتخاب کنید.\n5) یکی از این گزینه‌ها را بزنید:\n• تأیید و ارسال\n• ذخیره پیش‌نویس\n• زمان‌بندی\n• ذخیره قالب\n\nبرای خروج از هر مرحله /cancel را بفرستید.",
    "roles": "<b>👥 نقش‌ها و جریان تأیید</b>\n\n👑 <b>Sudo:</b> دسترسی کامل، مدیریت کاربران، تنظیمات و تأیید پست‌ها.\n\n⭐ <b>Owner:</b> مدیریت کاربران و کانال‌ها، قالب‌ها، گروه‌های کانال و تأیید پست‌ها.\n\n✏️ <b>Writer:</b> ساخت پست، ذخیره پیش‌نویس و مدیریت پست‌های خودش.\n\nتأیید پست‌ها به‌صورت پیش‌فرض خاموش است. اگر sudo یا owner آن را روشن کند، پست نویسنده قبل از انتشار در وضعیت «در انتظار تأیید» می‌ماند.",
    "settings": "<b>⚙️ تنظیمات و کانال‌ها</b>\n\nOwner یا Sudo از تنظیمات می‌تواند کانال Telegram یا Bale اضافه/حذف کند. ربات باید دسترسی ارسال داشته باشد.\n\n«تنظیم تأیید پست‌ها» مشخص می‌کند نویسندگان مستقیم منتشر کنند یا ابتدا منتظر تأیید بمانند.\n\nبرای گروه کانال:\n/group نام_گروه شناسه۱,شناسه۲\n\nبرای بررسی سلامت کانال‌ها /health را بفرستید.",
    "tools": "<b>📊 تاریخچه و ابزارها</b>\n\n📋 تاریخچه: مشاهده، ویرایش، حذف، کپی و ارسال مجدد.\n📑 قالب‌ها: /templates\n📑 استفاده از قالب: /use_template شناسه\n📊 گزارش آماری: /stats\n📋 گروه‌های کانال: /groups\n\nوضعیت‌های مهم: کامل، ناقص، ناموفق، پیش‌نویس و در انتظار تأیید.\n\nاگر پیام خطا یا ارسال ناقص دیدید، ابتدا «ارسال مجدد» را امتحان کنید.",
}


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE, section="main"):
    user_id = update.effective_user.id
    if not await is_writer_or_above(user_id):
        if update.callback_query:
            await update.callback_query.answer("❌ شما مجاز نیستید.", show_alert=True)
        else:
            await update.message.reply_text("❌ شما مجاز نیستید.")
        return
    markup = _help_keyboard() if section == "main" else InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ راهنمای اصلی", callback_data="help")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main")],
    ])
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(TEXT[section], parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.message.reply_text(TEXT[section], parse_mode=ParseMode.HTML, reply_markup=markup)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_help(update, context, "main")


async def handle_help_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    section = update.callback_query.data.removeprefix("help_")
    await show_help(update, context, section if section in TEXT else "main")
