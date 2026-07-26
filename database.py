import aiomysql
import json
from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME, SUDO_USER_ID

_pool: aiomysql.Pool | None = None


async def get_pool() -> aiomysql.Pool:
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            autocommit=True,
            charset="utf8mb4",
            minsize=1,
            maxsize=5,
        )
    return _pool


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    chat_type VARCHAR(50) DEFAULT 'channel',
                    platform VARCHAR(20) NOT NULL DEFAULT 'telegram',
                    is_active BOOLEAN DEFAULT TRUE,
                    last_health_status VARCHAR(20),
                    last_health_error TEXT,
                    last_health_check TIMESTAMP NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY unique_chat_platform (chat_id, platform)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            # Migration: add platform column if missing
            await cur.execute("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = %s AND table_name = 'channels' AND column_name = 'platform'
            """, (DB_NAME,))
            has_platform = (await cur.fetchone())[0]
            if not has_platform:
                await cur.execute("ALTER TABLE channels ADD COLUMN platform VARCHAR(20) NOT NULL DEFAULT 'telegram'")
                await cur.execute("ALTER TABLE channels DROP INDEX chat_id, ADD UNIQUE KEY unique_chat_platform (chat_id, platform)")
            for column, definition in (("last_health_status", "VARCHAR(20)"), ("last_health_error", "TEXT"), ("last_health_check", "TIMESTAMP NULL")):
                await cur.execute("""
                    SELECT COUNT(*) FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = 'channels' AND column_name = %s
                """, (DB_NAME, column))
                if not (await cur.fetchone())[0]:
                    await cur.execute(f"ALTER TABLE channels ADD COLUMN {column} {definition}")

            # Migration: fix charset for existing tables
            await cur.execute("""
                SELECT table_collation FROM information_schema.tables
                WHERE table_schema = %s AND table_name = 'channels'
            """, (DB_NAME,))
            row = await cur.fetchone()
            if row and row[0] != 'utf8mb4_unicode_ci':
                await cur.execute("ALTER TABLE channels CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")

            # Users table (replaces admins)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL UNIQUE,
                    role VARCHAR(20) NOT NULL DEFAULT 'writer',
                    name VARCHAR(255),
                    added_by BIGINT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            # Migration: migrate admins table to users
            await cur.execute("SHOW TABLES LIKE 'admins'")
            if await cur.fetchone():
                await cur.execute("SELECT user_id FROM admins")
                for row in await cur.fetchall():
                    uid = row[0]
                    if uid == SUDO_USER_ID:
                        await cur.execute(
                            "INSERT IGNORE INTO users (user_id, role, name) VALUES (%s, 'sudo', 'Sudo')",
                            (uid,),
                        )
                    else:
                        await cur.execute(
                            "INSERT IGNORE INTO users (user_id, role, name) VALUES (%s, 'owner', NULL)",
                            (uid,),
                        )
                await cur.execute("DROP TABLE admins")

            # Ensure sudo is always in users table
            await cur.execute("SELECT 1 FROM users WHERE user_id = %s", (SUDO_USER_ID,))
            if not await cur.fetchone():
                await cur.execute(
                    "INSERT IGNORE INTO users (user_id, role, name) VALUES (%s, 'sudo', 'Sudo')",
                    (SUDO_USER_ID,),
                )

            await cur.execute("""
                CREATE TABLE IF NOT EXISTS post_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    post_type VARCHAR(20) NOT NULL,
                    text TEXT,
                    file_id VARCHAR(512),
                    caption TEXT,
                    media_json TEXT,
                    tg_message_ids TEXT,
                    bale_result TEXT,
                    delivery_status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    delivery_error TEXT,
                    delivery_completed_at TIMESTAMP NULL,
                    target_channels_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_posts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    post_id INT NOT NULL,
                    run_at DATETIME NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'scheduled',
                    error TEXT,
                    processed_at TIMESTAMP NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_scheduled_due (status, run_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)

            await cur.execute("""
                CREATE TABLE IF NOT EXISTS post_versions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    post_id INT NOT NULL,
                    version_no INT NOT NULL,
                    text TEXT,
                    caption TEXT,
                    changed_by BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY unique_post_version (post_id, version_no)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_groups (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    owner_id BIGINT NOT NULL,
                    name VARCHAR(150) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY unique_group_owner_name (owner_id, name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_group_members (
                    group_id INT NOT NULL,
                    channel_id INT NOT NULL,
                    PRIMARY KEY (group_id, channel_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    setting_key VARCHAR(80) PRIMARY KEY,
                    setting_value VARCHAR(255) NOT NULL,
                    updated_by BIGINT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            await cur.execute("INSERT IGNORE INTO bot_settings (setting_key, setting_value, updated_by) VALUES ('approval_required', '0', %s)", (SUDO_USER_ID,))
            await cur.execute("DROP TABLE IF EXISTS templates")
            await cur.execute("SELECT setting_value FROM bot_settings WHERE setting_key = 'legacy_delivery_migrated'")
            if not await cur.fetchone():
                # Older releases incorrectly labelled already-published rows as pending.
                await cur.execute("UPDATE post_history SET delivery_status = 'completed', delivery_completed_at = COALESCE(delivery_completed_at, created_at) WHERE delivery_status IN ('pending', 'pending_approval')")
                await cur.execute("INSERT INTO bot_settings (setting_key, setting_value, updated_by) VALUES ('legacy_delivery_migrated', '1', %s)", (SUDO_USER_ID,))
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS role_permissions (
                    role VARCHAR(20) NOT NULL,
                    permission VARCHAR(60) NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    PRIMARY KEY (role, permission)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            await cur.executemany(
                "INSERT IGNORE INTO role_permissions (role, permission) VALUES (%s, %s)",
                [("sudo", p) for p in ("publish", "approve", "manage_users", "manage_channels", "manage_groups", "view_analytics")] +
                [("owner", p) for p in ("publish", "approve", "manage_users", "manage_channels", "manage_groups", "view_analytics")] +
                [("writer", "create_draft")],
            )

            # Migration for installations created before delivery tracking existed.
            for column, definition in (
                ("delivery_status", "VARCHAR(20) NOT NULL DEFAULT 'pending'"),
                ("delivery_error", "TEXT"),
                ("delivery_completed_at", "TIMESTAMP NULL"),
                ("target_channels_json", "TEXT"),
            ):
                await cur.execute("""
                    SELECT COUNT(*) FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = 'post_history' AND column_name = %s
                """, (DB_NAME, column))
                if not (await cur.fetchone())[0]:
                    await cur.execute(f"ALTER TABLE post_history ADD COLUMN {column} {definition}")


# --- User/Role functions ---

async def get_user_role(user_id: int) -> str | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT role FROM users WHERE user_id = %s", (user_id,))
            row = await cur.fetchone()
            return row[0] if row else None


async def is_sudo(user_id: int) -> bool:
    return user_id == SUDO_USER_ID


async def is_owner(user_id: int) -> bool:
    role = await get_user_role(user_id)
    return role in ("sudo", "owner")


async def is_writer_or_above(user_id: int) -> bool:
    role = await get_user_role(user_id)
    return role is not None


async def add_user(user_id: int, role: str, name: str = None, added_by: int = None) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO users (user_id, role, name, added_by) VALUES (%s, %s, %s, %s)",
                    (user_id, role, name, added_by),
                )
                return True
            except aiomysql.IntegrityError:
                return False


async def update_user_role(user_id: int, role: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE users SET role = %s WHERE user_id = %s", (role, user_id))


async def remove_user(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM users WHERE user_id = %s AND role != 'sudo'", (user_id,))


async def get_all_users() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT user_id, role, name, created_at FROM users ORDER BY FIELD(role, 'sudo', 'owner', 'writer')")
            return list(await cur.fetchall())


async def get_user_name(user_id: int) -> str | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT name FROM users WHERE user_id = %s", (user_id,))
            row = await cur.fetchone()
            return row[0] if row else None


# --- Post permission checks ---

async def can_edit_post(user_id: int, post_id: int) -> bool:
    role = await get_user_role(user_id)
    if role in ("sudo", "owner"):
        return True
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT user_id FROM post_history WHERE id = %s", (post_id,))
            row = await cur.fetchone()
            return row and row[0] == user_id


async def can_delete_post(user_id: int, post_id: int) -> bool:
    return await can_edit_post(user_id, post_id)


async def get_setting(key: str, default: str = None) -> str | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT setting_value FROM bot_settings WHERE setting_key = %s", (key,))
            row = await cur.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str, updated_by: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO bot_settings (setting_key, setting_value, updated_by) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value), updated_by = VALUES(updated_by)", (key, value, updated_by))


async def has_permission(user_id: int, permission: str) -> bool:
    role = await get_user_role(user_id)
    if not role:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT enabled FROM role_permissions WHERE role = %s AND permission = %s", (role, permission))
            row = await cur.fetchone()
            return bool(row and row[0])


async def save_post_version(post_id: int, changed_by: int, text: str, caption: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COALESCE(MAX(version_no), 0) + 1 FROM post_versions WHERE post_id = %s", (post_id,))
            version = (await cur.fetchone())[0]
            await cur.execute("INSERT INTO post_versions (post_id, version_no, text, caption, changed_by) VALUES (%s, %s, %s, %s, %s)", (post_id, version, text, caption, changed_by))
            return version


async def get_post_versions(post_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM post_versions WHERE post_id = %s ORDER BY version_no DESC", (post_id,))
            return list(await cur.fetchall())


async def create_channel_group(owner_id: int, name: str, channel_ids: list[int]) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute("INSERT INTO channel_groups (owner_id, name) VALUES (%s, %s)", (owner_id, name))
                group_id = cur.lastrowid
                await cur.executemany("INSERT INTO channel_group_members (group_id, channel_id) VALUES (%s, %s)", [(group_id, cid) for cid in channel_ids])
                return True
            except aiomysql.IntegrityError:
                return False


async def get_channel_groups(owner_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT g.id, g.name, COUNT(m.channel_id) AS channel_count FROM channel_groups g LEFT JOIN channel_group_members m ON m.group_id = g.id WHERE g.owner_id = %s GROUP BY g.id ORDER BY g.name", (owner_id,))
            return list(await cur.fetchall())


async def get_group_channel_ids(group_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT channel_id FROM channel_group_members WHERE group_id = %s", (group_id,))
            return [row[0] for row in await cur.fetchall()]


# --- Channel functions ---

async def get_active_channels(platform: str = None) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            if platform:
                await cur.execute(
                    "SELECT id, chat_id, name, chat_type, platform FROM channels WHERE is_active = TRUE AND platform = %s",
                    (platform,),
                )
            else:
                await cur.execute(
                    "SELECT id, chat_id, name, chat_type, platform FROM channels WHERE is_active = TRUE"
                )
            return list(await cur.fetchall())


async def add_channel(chat_id: int, name: str, chat_type: str = "channel", platform: str = "telegram") -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO channels (chat_id, name, chat_type, platform) VALUES (%s, %s, %s, %s)",
                    (chat_id, name, chat_type, platform),
                )
                return True
            except aiomysql.IntegrityError:
                return False


async def remove_channel(channel_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM channels WHERE id = %s", (channel_id,))


async def channel_exists(chat_id: int, platform: str = "telegram") -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM channels WHERE chat_id = %s AND platform = %s",
                (chat_id, platform),
            )
            return await cur.fetchone() is not None


async def update_channel_health(channel_id: int, status: str, error: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE channels SET last_health_status = %s, last_health_error = %s, last_health_check = CURRENT_TIMESTAMP WHERE id = %s", (status, error, channel_id))


async def get_analytics():
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            result = {}
            await cur.execute("SELECT COUNT(*) AS total, SUM(delivery_status = 'completed') AS completed, SUM(delivery_status = 'partial') AS partial, SUM(delivery_status = 'failed') AS failed, SUM(delivery_status = 'draft') AS drafts, SUM(delivery_status = 'pending_approval') AS approvals FROM post_history")
            result["posts"] = await cur.fetchone()
            await cur.execute("SELECT COUNT(*) AS total, SUM(is_active = TRUE) AS active FROM channels")
            result["channels"] = await cur.fetchone()
            await cur.execute("SELECT platform, COUNT(*) AS total FROM channels WHERE is_active = TRUE GROUP BY platform")
            result["platforms"] = list(await cur.fetchall())
            await cur.execute("SELECT user_id, COUNT(*) AS total FROM post_history GROUP BY user_id ORDER BY total DESC LIMIT 5")
            result["authors"] = list(await cur.fetchall())
            await cur.execute("SELECT COUNT(*) AS total FROM post_history WHERE created_at >= NOW() - INTERVAL 24 HOUR")
            result["last_24h"] = (await cur.fetchone())["total"]
            return result


async def get_channel_health():
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT id, chat_id, name, platform, is_active, last_health_status, last_health_error, last_health_check FROM channels ORDER BY platform, name")
            return list(await cur.fetchall())


# --- Post history functions ---

async def save_post(user_id: int, post_type: str, text: str = None, file_id: str = None,
                    caption: str = None, media_json: str = None, target_channels_json: str = None,
                    delivery_status: str = "pending") -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO post_history (user_id, post_type, text, file_id, caption, media_json, target_channels_json, delivery_status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (user_id, post_type, text, file_id, caption, media_json, target_channels_json, delivery_status),
            )
            return cur.lastrowid


async def update_post_message_ids(post_id: int, tg_message_ids: str, bale_result: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE post_history SET tg_message_ids = %s, bale_result = %s WHERE id = %s",
                (tg_message_ids, bale_result, post_id),
            )


async def update_post_status(post_id: int, status: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE post_history SET delivery_status = %s WHERE id = %s", (status, post_id))


async def update_post_delivery(post_id: int, status: str, error: str = None):
    """Record the final aggregate delivery result for a post."""
    if status not in {"pending", "completed", "partial", "failed"}:
        raise ValueError(f"Invalid delivery status: {status}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE post_history SET delivery_status = %s, delivery_error = %s, "
                "delivery_completed_at = CURRENT_TIMESTAMP WHERE id = %s",
                (status, error, post_id),
            )


async def create_schedule(user_id: int, post_id: int, run_at) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO scheduled_posts (user_id, post_id, run_at, status) VALUES (%s, %s, %s, 'scheduled')",
                (user_id, post_id, run_at),
            )
            return cur.lastrowid


async def get_due_schedules():
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM scheduled_posts WHERE status = 'scheduled' AND run_at <= UTC_TIMESTAMP() ORDER BY run_at LIMIT 50")
            return list(await cur.fetchall())


async def update_schedule(schedule_id: int, status: str, error: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE scheduled_posts SET status = %s, error = %s, processed_at = CURRENT_TIMESTAMP WHERE id = %s", (status, error, schedule_id))


async def get_user_posts(user_id: int, limit: int = 10) -> list[dict]:
    """Get posts for a writer (only their own)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, post_type, text, caption, created_at FROM post_history WHERE user_id = %s ORDER BY id DESC LIMIT %s",
                (user_id, limit),
            )
            return list(await cur.fetchall())


async def get_user_posts_paginated(user_id: int, limit: int, offset: int) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, post_type, text, caption, created_at FROM post_history WHERE user_id = %s ORDER BY id DESC LIMIT %s OFFSET %s",
                (user_id, limit, offset),
            )
            return list(await cur.fetchall())


async def get_all_posts(limit: int = 20) -> list[dict]:
    """Get all posts (for sudo/owner)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, user_id, post_type, text, caption, created_at FROM post_history ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            return list(await cur.fetchall())


async def get_all_posts_paginated(limit: int, offset: int) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, user_id, post_type, text, caption, created_at FROM post_history ORDER BY id DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
            return list(await cur.fetchall())


async def count_user_posts(user_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM post_history WHERE user_id = %s", (user_id,))
            row = await cur.fetchone()
            return row[0] if row else 0


async def count_all_posts() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM post_history")
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_post(post_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM post_history WHERE id = %s", (post_id,)
            )
            return await cur.fetchone()


async def update_post_caption(post_id: int, caption: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE post_history SET caption = %s WHERE id = %s",
                (caption, post_id),
            )


async def update_post_text(post_id: int, text: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE post_history SET text = %s WHERE id = %s",
                (text, post_id),
            )


async def delete_post(post_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM post_history WHERE id = %s", (post_id,))
