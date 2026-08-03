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
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.constants import ParseMode

from config import BOT_TOKEN as _CONFIRM_TOKEN, DISPLAY_TIMEZONE  # noqa: F811
from database import init_db, close_pool
from utils import GROUP_NOTICE, is_private_chat, private_actor
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
    handle_schedule_back_date,
    handle_schedule_back_hour,
    handle_schedule_calendar,
    handle_schedule_noop,
    handle_legacy_schedule_button,
    process_scheduled_posts,
    process_delivery_retries,
    restore_workflow_states,
    handle_any_message,
)
from handlers.schedules import (
    handle_scheduled_list,
    handle_schedule_view,
    handle_schedule_cancel,
    handle_schedule_now,
    handle_schedule_time_change,
    handle_reschedule_input,
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
    handle_calendar_settings,
    handle_toggle_calendar,
    load_calendar_preference,
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
from handlers.admin import handle_health, handle_tools_menu, run_channel_health_checks
from handlers.stats import (
    handle_stats_menu, handle_stats_summary, handle_stats_trend,
    handle_stats_channels, handle_stats_authors, handle_stats_schedule,
    daily_report,
)
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


async def block_non_private(update, context):
    """Drop every update that does not come from a private chat.

    Registered in group -1 so it runs before all other handlers and raises
    ApplicationHandlerStop, which prevents any later handler from seeing the
    update. This is the single choke point for the private-chat rule:

    * MessageHandler filters cannot cover callback queries — CallbackQueryHandler
      ignores message filters entirely, so every inline button (new post, settings,
      users, backup, ...) was reachable from a group by tapping a menu the bot had
      posted there via /help.
    * Per-handler checks are easy to forget; a new command or button would silently
      reopen the hole. Enforcing it here means a handler cannot opt out by accident.
    """
    if is_private_chat(update):
        return
    # Answer taps so the client stops spinning, and tell the user where to go.
    query = getattr(update, "callback_query", None)
    if query is not None:
        try:
            await query.answer(GROUP_NOTICE, show_alert=True)
        except Exception:
            logger.debug("Could not answer callback query from non-private chat", exc_info=True)
    raise ApplicationHandlerStop


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

    # Tell everyone whose workflow survived the restart that they can carry on.
    # The restart handler asked them to wait, so it has to release them too.
    from handlers.post import user_states
    from config import SUDO_USER_ID as _sudo
    for uid, state in list(user_states.items()):
        if not state.get("restored"):
            continue
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="🟢 ربات دوباره در دسترس است.\n"
                     "کار قبلی شما بازیابی شد؛ می‌توانید ادامه دهید. "
                     "برای شروع دوباره /cancel را بزنید.",
            )
        except Exception:
            logger.debug("Could not notify restored user %s", uid, exc_info=True)
        state.pop("restored", None)


async def shutdown_database(application):
    await close_pool()


async def on_error(update, context):
    """Global error handler.

    Without one, python-telegram-bot only logs ("No error handlers are
    registered") and the user is left staring at a spinner that never resolves.
    """
    logger.error("Unhandled exception while processing update", exc_info=context.error)

    # Always release the button the user pressed, otherwise the client spins.
    query = getattr(update, "callback_query", None) if update else None
    if query is not None:
        try:
            await query.answer("❌ خطایی رخ داد. دوباره تلاش کنید.", show_alert=True)
        except Exception:
            pass
        return

    message = getattr(update, "effective_message", None) if update else None
    if message is not None:
        try:
            await message.reply_text(
                "❌ خطایی رخ داد. اگر ادامه داشت /cancel را بزنید و دوباره تلاش کنید."
            )
        except Exception:
            pass


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set. Check your .env file.")

    app = ApplicationBuilder().token(BOT_TOKEN).post_shutdown(shutdown_database).build()

    # Global private-chat gate. Group -1 runs before every other group, and the
    # handler raises ApplicationHandlerStop, so nothing below can ever be reached
    # from a group, supergroup or channel — commands and inline buttons included.
    app.add_handler(TypeHandler(Update, block_non_private), group=-1)

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", handle_cancel_command))
    app.add_handler(CommandHandler("group", handle_group_command))
    app.add_handler(CommandHandler("groups", handle_groups_command))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("stats", handle_stats_menu))
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
    # Dates travel as ISO strings so a tapped button is unambiguous even if the
    # in-memory state has moved on. Order matters: the more specific
    # "schedule_back_hour_" pattern must be registered before "schedule_hour_".
    app.add_handler(CallbackQueryHandler(handle_schedule_back_date, pattern="^schedule_back_date$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_calendar, pattern=r"^schedule_cal(_\d{3,4}_\d{1,2})?$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_back_hour, pattern=r"^schedule_back_hour_\d{4}-\d{2}-\d{2}$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_date, pattern=r"^schedule_date_\d{4}-\d{2}-\d{2}$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_hour, pattern=r"^schedule_hour_\d{4}-\d{2}-\d{2}_\d{1,2}$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_minute, pattern=r"^schedule_minute_\d{4}-\d{2}-\d{2}_\d{1,2}_\d{1,2}$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_noop, pattern="^schedule_noop$"))
    # Buttons rendered by the previous release are still on users' screens;
    # absorb them instead of leaving the client spinning. Registered last so it
    # only ever catches payloads the real handlers above did not match.
    app.add_handler(CallbackQueryHandler(
        handle_legacy_schedule_button,
        pattern=r"^schedule_(date_(today|tomorrow)|hour_\d+|minute_\d+_\d+)$",
    ))
    app.add_handler(CallbackQueryHandler(handle_schedule_post, pattern="^schedule_post$"))
    # Managing existing schedules
    app.add_handler(CallbackQueryHandler(handle_scheduled_list, pattern="^scheduled_list$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_view, pattern=r"^sched_view_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_cancel, pattern=r"^sched_cancel_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_now, pattern=r"^sched_now_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_schedule_time_change, pattern=r"^sched_time_\d+$"))
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
    app.add_handler(CallbackQueryHandler(handle_stats_menu, pattern="^tools_stats$"))
    app.add_handler(CallbackQueryHandler(handle_stats_summary, pattern="^stats_summary$"))
    app.add_handler(CallbackQueryHandler(handle_stats_trend, pattern="^stats_trend$"))
    app.add_handler(CallbackQueryHandler(handle_stats_channels, pattern="^stats_channels$"))
    app.add_handler(CallbackQueryHandler(handle_stats_authors, pattern="^stats_authors$"))
    app.add_handler(CallbackQueryHandler(handle_stats_schedule, pattern="^stats_schedule$"))
    app.add_handler(CallbackQueryHandler(handle_health, pattern="^tools_health$"))
    app.add_handler(CallbackQueryHandler(handle_help, pattern="^help$"))
    app.add_handler(CallbackQueryHandler(handle_help_section, pattern="^help_(publish|schedule|roles|settings|tools)$"))
    app.add_handler(CallbackQueryHandler(handle_approval_settings, pattern="^approval_settings$"))
    app.add_handler(CallbackQueryHandler(handle_toggle_approval, pattern="^toggle_approval$"))
    app.add_handler(CallbackQueryHandler(handle_calendar_settings, pattern="^calendar_settings$"))
    app.add_handler(CallbackQueryHandler(handle_toggle_calendar, pattern="^toggle_calendar$"))
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
        # Reject every non-private message. Messages from groups, supergroups and
        # channels must never trigger post creation, editing, channel adding,
        # user adding or restore workflows.
        #
        # private_actor also rejects updates with no effective_user (anonymous
        # channel posts / service messages) and no message (edited messages),
        # both of which reach this handler via filters.ALL and used to raise
        # AttributeError before any chat-type check could run.
        if private_actor(update) is None:
            return
        # Restore uploads must be handled before normal workflow routing.
        if await handle_restore_document(update, context):
            return
        from handlers.users import handle_add_user_input
        # Try user input first
        if await handle_add_user_input(update, context):
            return
        # Reschedule time entry for an existing schedule
        if await handle_reschedule_input(update, context):
            return
        # Try edit input
        if await handle_edit_input(update, context):
            return
        # Try settings channel input
        if await handle_channel_input(update, context):
            return
        # Then try post handlers
        await handle_any_message(update, context)

    # filters.ChatType.PRIVATE stops group/supergroup/channel updates at the
    # dispatcher, so route_message is only ever invoked for private chats.
    # The in-function check above stays as defence in depth.
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.ALL & ~filters.COMMAND, route_message)
    )

    app.add_error_handler(on_error)

    # Startup sequence. The periodic jobs are registered *after* init_db has
    # actually finished rather than on a hopeful 10-second delay — migrations
    # or a slow/retrying MySQL used to race the first scheduler tick.
    async def initialize_database(context):
        await init_db()

        # Apply the owner's saved calendar preference before anything renders.
        try:
            await load_calendar_preference()
        except Exception:
            logger.exception("Could not load calendar preference")

        # Recover interactive workflows that a restart interrupted.
        try:
            restored = await restore_workflow_states(context)
            if restored:
                logger.info("Restored %d interrupted workflow(s)", restored)
        except Exception:
            logger.exception("Workflow restoration failed")

        context.job_queue.run_repeating(
            process_scheduled_posts, interval=60, first=5, name="scheduled_posts",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 300},
        )
        context.job_queue.run_repeating(
            process_delivery_retries, interval=300, first=60, name="delivery_retries",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 600},
        )
        context.job_queue.run_repeating(
            run_channel_health_checks, interval=900, first=30, name="channel_health",
            job_kwargs={"max_instances": 1, "coalesce": True},
        )
        context.job_queue.run_repeating(daily_report, interval=86400, first=86400, name="daily_report")
        context.job_queue.run_daily(
            nightly_backup,
            time=dt_time(23, 59, tzinfo=ZoneInfo(DISPLAY_TIMEZONE)),
            name="nightly_backup",
        )
        logger.info("Database ready; periodic jobs scheduled")

    app.job_queue.run_once(initialize_database, when=0)
    app.job_queue.run_once(notify_online, when=2)

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
