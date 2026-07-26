import html
import logging
import os
import sys
import threading
from datetime import time as dt_time
from zoneinfo import ZoneInfo

# --- Load env and bot token BEFORE anything else ---
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BALE_TOKEN = os.getenv("BALE_TOKEN")
LOG_CHANNEL_ID = -5061365940

# --- Telegram Log Handler (set up first so import errors are captured) ---
def _send_log_sync(text: str):
    try:
        from urllib.request import urlopen
        from urllib.parse import urlencode
        data = urlencode({"chat_id": LOG_CHANNEL_ID, "text": html.escape(text), "parse_mode": "HTML"}).encode()
        urlopen(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data, timeout=10)
    except Exception:
        pass


class TelegramLogHandler(logging.Handler):
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)

    def emit(self, record):
        try:
            msg = self.format(record)
            if len(msg) > 4000:
                msg = msg[:4000] + "\n... (truncated)"
            t = threading.Thread(target=_send_log_sync, args=(msg,), daemon=True)
            t.start()
        except Exception:
            pass


# Setup logging immediately
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)

if BOT_TOKEN:
    tg_handler = TelegramLogHandler(level=logging.WARNING)
    tg_handler.setFormatter(logging.Formatter("%(asctime)s\n%(name)s - %(levelname)s\n\n%(message)s"))
    logging.getLogger().addHandler(tg_handler)

logger = logging.getLogger(__name__)


def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = handle_exception


# --- Now import everything else (errors will be logged) ---
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

from config import BOT_TOKEN as _CONFIRM_TOKEN  # noqa: F811
from database import init_db, close_pool
from handlers.start import start
from handlers.post import (
    handle_confirm_post,
    handle_cancel_post,
    handle_cancel_command,
    handle_new_post,
    handle_choose_channels,
    handle_toggle_channel,
    handle_channels_done,
    handle_channels_back,
    handle_save_draft,
    handle_schedule_post,
    handle_schedule_date,
    handle_schedule_hour,
    handle_schedule_minute,
    process_scheduled_posts,
    handle_any_message,
)
from handlers.settings import (
    handle_settings,
    handle_manage_channels,
    handle_add_channel,
    handle_add_bale_channel,
    handle_channel_input,
    handle_remove_channel,
    handle_back_main,
    handle_bot_status,
    handle_approval_settings,
    handle_toggle_approval,
    handle_bot_restart,
    handle_do_restart,
    handle_group_command,
    handle_groups_command,
    handle_groups_menu,
    handle_create_group,
)
from handlers.history import (
    handle_history,
    handle_history_noop,
    handle_post_detail,
    handle_edit,
    handle_edit_input,
    handle_delete,
    handle_duplicate,
    handle_retry,
    handle_publish_draft,
    handle_approve,
)
from handlers.admin import handle_stats, handle_health, handle_tools_menu, run_channel_health_checks, daily_report
from handlers.help import handle_help, handle_help_section
from backup import handle_backup, handle_restore, handle_restore_document, nightly_backup
from handlers.users import (
    handle_users_menu,
    handle_add_user,
    handle_add_user_input,
    handle_role_select,
    handle_list_users,
    handle_user_info,
    handle_promote_owner,
    handle_demote_writer,
    handle_remove_user,
)


async def notify_online(context):
    try:
        from config import SUDO_USER_ID
        await context.bot.send_message(
            chat_id=SUDO_USER_ID,
            text="🟢 <b>ربات آنلاین شد و آماده به کار است.</b>",
            parse_mode=ParseMode.HTML
        )
        logger.info("Online notification sent to sudo user %s", SUDO_USER_ID)
    except Exception as e:
        logger.error("Failed to send online notification: %s", e)


async def shutdown_database(application):
    await close_pool()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set. Check your .env file.")

    app = ApplicationBuilder().token(BOT_TOKEN).post_shutdown(shutdown_database).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", handle_cancel_command))
    app.add_handler(CommandHandler("group", handle_group_command))
    app.add_handler(CommandHandler("groups", handle_groups_command))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("stats", handle_stats))
    app.add_handler(CommandHandler("health", handle_health))

    # Callback query handlers (inline buttons)
    app.add_handler(CallbackQueryHandler(handle_new_post, pattern="^new_post$"))
    app.add_handler(CallbackQueryHandler(handle_history, pattern="^history$"))
    app.add_handler(CallbackQueryHandler(handle_history, pattern="^history_page_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_history_noop, pattern="^history_noop$"))
    app.add_handler(CallbackQueryHandler(handle_post_detail, pattern="^post_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_edit, pattern="^edit_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_delete, pattern="^delete_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_duplicate, pattern="^duplicate_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_retry, pattern="^retry_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_publish_draft, pattern="^publish_draft_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_approve, pattern="^approve_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_settings, pattern="^settings$"))
    app.add_handler(CallbackQueryHandler(handle_manage_channels, pattern="^manage_channels$"))
    app.add_handler(CallbackQueryHandler(handle_backup, pattern="^backup_project$"))
    app.add_handler(CallbackQueryHandler(handle_restore, pattern="^restore_project$"))
    app.add_handler(CallbackQueryHandler(handle_confirm_post, pattern="^confirm_post$"))
    app.add_handler(CallbackQueryHandler(handle_cancel_post, pattern="^cancel_post$"))
    app.add_handler(CallbackQueryHandler(handle_choose_channels, pattern="^choose_channels$"))
    app.add_handler(CallbackQueryHandler(handle_toggle_channel, pattern="^toggle_channel_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_channels_done, pattern="^channels_done$"))
    app.add_handler(CallbackQueryHandler(handle_channels_back, pattern="^channels_back$"))
    app.add_handler(CallbackQueryHandler(handle_save_draft, pattern="^save_draft$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_date, pattern="^schedule_date_(today|tomorrow)$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_hour, pattern="^schedule_hour_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_minute, pattern="^schedule_minute_\\d+_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_post, pattern="^schedule_post$"))
    app.add_handler(CallbackQueryHandler(handle_add_channel, pattern="^add_channel$"))
    app.add_handler(CallbackQueryHandler(handle_add_bale_channel, pattern="^add_bale_channel$"))
    app.add_handler(CallbackQueryHandler(handle_remove_channel, pattern="^remove_\\d+$"))
    app.add_handler(CallbackQueryHandler(handle_bot_status, pattern="^bot_status$"))
    app.add_handler(CallbackQueryHandler(handle_bot_restart, pattern="^bot_restart$"))
    app.add_handler(CallbackQueryHandler(handle_do_restart, pattern="^do_restart$"))
    app.add_handler(CallbackQueryHandler(handle_back_main, pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(handle_tools_menu, pattern="^tools_menu$"))
    app.add_handler(CallbackQueryHandler(handle_groups_menu, pattern="^tools_groups$"))
    app.add_handler(CallbackQueryHandler(handle_create_group, pattern="^create_group$"))
    app.add_handler(CallbackQueryHandler(handle_stats, pattern="^tools_stats$"))
    app.add_handler(CallbackQueryHandler(handle_health, pattern="^tools_health$"))
    app.add_handler(CallbackQueryHandler(handle_help, pattern="^help$"))
    app.add_handler(CallbackQueryHandler(handle_help_section, pattern="^help_(publish|roles|settings|tools)$"))
    app.add_handler(CallbackQueryHandler(handle_approval_settings, pattern="^approval_settings$"))
    app.add_handler(CallbackQueryHandler(handle_toggle_approval, pattern="^toggle_approval$"))
    # User management handlers
    app.add_handler(CallbackQueryHandler(handle_users_menu, pattern="^users_menu$"))
    app.add_handler(CallbackQueryHandler(handle_add_user, pattern="^add_user$"))
    app.add_handler(CallbackQueryHandler(handle_role_select, pattern="^role_"))
    app.add_handler(CallbackQueryHandler(handle_list_users, pattern="^list_users$"))
    app.add_handler(CallbackQueryHandler(handle_user_info, pattern="^user_info_"))
    app.add_handler(CallbackQueryHandler(handle_promote_owner, pattern="^promote_owner_"))
    app.add_handler(CallbackQueryHandler(handle_demote_writer, pattern="^demote_writer_"))
    app.add_handler(CallbackQueryHandler(handle_remove_user, pattern="^remove_user_"))

    # Message handler for post content + channel input
    # Must be last so callbacks are matched first
    async def route_message(update, context):
        # Restore uploads must be handled before normal workflow routing.
        if await handle_restore_document(update, context):
            return
        from handlers.users import handle_add_user_input
        # Try user input first
        if await handle_add_user_input(update, context):
            return
        # Try edit input
        if await handle_edit_input(update, context):
            return
        # Try settings channel input
        if await handle_channel_input(update, context):
            return
        # Then try post handlers
        await handle_any_message(update, context)

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, route_message))

    # Initialize database and send online notification
    async def initialize_database(context):
        await init_db()

    app.job_queue.run_once(initialize_database, when=0)
    app.job_queue.run_once(notify_online, when=1)
    app.job_queue.run_repeating(process_scheduled_posts, interval=60, first=10, name="scheduled_posts")
    app.job_queue.run_repeating(run_channel_health_checks, interval=900, first=30, name="channel_health")
    app.job_queue.run_repeating(daily_report, interval=86400, first=86400, name="daily_report")
    app.job_queue.run_daily(nightly_backup, time=dt_time(23, 59, tzinfo=ZoneInfo("Asia/Tehran")), name="nightly_backup")

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
