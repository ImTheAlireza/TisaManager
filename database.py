import aiomysql
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)


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


# --- Post history functions ---

async def save_post(user_id: int, post_type: str, text: str = None, file_id: str = None,
                    caption: str = None, media_json: str = None) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO post_history (user_id, post_type, text, file_id, caption, media_json) VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, post_type, text, file_id, caption, media_json),
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
