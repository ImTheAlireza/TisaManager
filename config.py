import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BALE_TOKEN = os.getenv("BALE_TOKEN")
SUDO_USER_ID = int(os.getenv("SUDO_USER_ID", 0))
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "post_manager_bot")

# --- Scheduling / delivery tuning -------------------------------------------
# Every user-facing time is rendered in this zone; every stored time is UTC.
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "Asia/Tehran")

# Show dates on the Persian (Jalali) calendar. The whole UI is Persian, so this
# defaults on; set USE_JALALI=0 for a Gregorian UI. Storage is always Gregorian
# UTC either way.
USE_JALALI = os.getenv("USE_JALALI", "1") not in ("0", "false", "False", "")

# How long a schedule may be overdue and still be published. Anything older is
# marked "expired" instead of being blasted out after a long outage.
SCHEDULE_GRACE_SECONDS = int(os.getenv("SCHEDULE_GRACE_SECONDS", 6 * 3600))

# A row claimed for publishing but never finished (process killed mid-send) is
# handed back to the queue after this long.
SCHEDULE_CLAIM_TIMEOUT_SECONDS = int(os.getenv("SCHEDULE_CLAIM_TIMEOUT_SECONDS", 900))

# A schedule that keeps dying mid-publish is abandoned after this many claims.
SCHEDULE_MAX_ATTEMPTS = int(os.getenv("SCHEDULE_MAX_ATTEMPTS", 3))

# Automatic re-delivery offsets (hours after the first failure) for channels
# that failed. Empty string disables automatic retries.
RETRY_DELAYS_HOURS = tuple(
    int(x) for x in os.getenv("RETRY_DELAYS_HOURS", "1,3,6").split(",") if x.strip()
)

# An interactive workflow (composing a post, picking a time) is remembered for
# this long, and survives a restart.
WORKFLOW_TTL_SECONDS = int(os.getenv("WORKFLOW_TTL_SECONDS", 30 * 60))

# How long a restart waits for in-flight publishes to finish before going down.
RESTART_DRAIN_TIMEOUT_SECONDS = int(os.getenv("RESTART_DRAIN_TIMEOUT_SECONDS", 60))
