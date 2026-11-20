import os
import aiosqlite
from datetime import datetime, timezone, timedelta
from config import (
    DEFAULT_LIFETIME_MINUTES, EXTENSION_INCREMENT_MINUTES,
    MAX_TOTAL_LIFETIME_MINUTES, MAX_EXTENSIONS_PER_MAILBOX,
    RATE_LIMIT_COOLDOWN_SECONDS, HOURLY_CREATION_LIMIT, DAILY_CREATION_LIMIT,
    INBOX_PAGE_SIZE, IST
)

DB_PATH = os.path.join(os.path.dirname(__file__), "kayimail.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode = WAL;")
        await db.execute("PRAGMA busy_timeout = 5000;")
        await db.execute("PRAGMA foreign_keys = ON;")

        # Users table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP NOT NULL,
                last_active_at TIMESTAMP NOT NULL
            )
        """)

        # Mailboxes table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mailboxes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'guerrillamail',
                account_id TEXT NOT NULL,
                token TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'active',
                extension_count INTEGER DEFAULT 0,
                last_polled_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id) ON DELETE CASCADE
            )
        """)

        # Messages table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mailbox_id INTEGER NOT NULL,
                provider_message_id TEXT NOT NULL,
                sender_address TEXT,
                sender_name TEXT,
                subject TEXT,
                received_at TIMESTAMP NOT NULL,
                is_read INTEGER DEFAULT 0,
                notified_at TIMESTAMP,
                otp_code TEXT,
                body_text TEXT,
                UNIQUE (mailbox_id, provider_message_id),
                FOREIGN KEY (mailbox_id) REFERENCES mailboxes (id) ON DELETE CASCADE
            )
        """)

        # Rate limits table (sliding window tracker)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rate_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
        """)

        # Performance Indexes
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mailboxes_user_status ON mailboxes (user_id, status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mailboxes_expires ON mailboxes (expires_at, status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_mailbox ON messages (mailbox_id, id DESC);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_user ON rate_limits (user_id, action, created_at);")

        await db.commit()

async def touch_user(user_id: int):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (telegram_id, created_at, last_active_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET last_active_at = excluded.last_active_at
        """, (user_id, now, now))
        await db.commit()

async def check_generation_limits(user_id: int) -> tuple[bool, str, int]:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    one_hour_ago = (now - timedelta(hours=1)).isoformat()
    one_day_ago = (now - timedelta(days=1)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Check last generation for 60s cooldown
        async with db.execute(
            "SELECT created_at FROM rate_limits WHERE user_id = ? AND action = 'create_mailbox' ORDER BY id DESC LIMIT 1",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                last_dt = datetime.fromisoformat(row[0])
                elapsed = (now - last_dt).total_seconds()
                if elapsed < RATE_LIMIT_COOLDOWN_SECONDS:
                    return False, "cooldown", int(RATE_LIMIT_COOLDOWN_SECONDS - elapsed)

        # 2. Check rolling 1 hour limit (max 5)
        async with db.execute(
            "SELECT COUNT(*) FROM rate_limits WHERE user_id = ? AND action = 'create_mailbox' AND created_at > ?",
            (user_id, one_hour_ago)
        ) as cursor:
            count_hour = (await cursor.fetchone())[0]
            if count_hour >= HOURLY_CREATION_LIMIT:
                return False, "hourly", 3600

        # 3. Check rolling 24 hours limit (max 15)
        async with db.execute(
            "SELECT COUNT(*) FROM rate_limits WHERE user_id = ? AND action = 'create_mailbox' AND created_at > ?",
            (user_id, one_day_ago)
        ) as cursor:
            count_day = (await cursor.fetchone())[0]
            if count_day >= DAILY_CREATION_LIMIT:
                return False, "daily", 86400

    return True, "ok", 0

async def get_active_mailbox(user_id: int) -> dict | None:
    now = datetime.now(timezone.utc)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM mailboxes WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None

            # Check expiration
            exp_dt = datetime.fromisoformat(row["expires_at"])
            if exp_dt <= now:
                await db.execute("UPDATE mailboxes SET status = 'expired' WHERE id = ?", (row["id"],))
                await db.commit()
                return None

            return dict(row)

async def save_new_mailbox(user_id: int, address: str, provider: str, account_id: str, token: str) -> dict:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=DEFAULT_LIFETIME_MINUTES)
    now_iso = now.isoformat()
    exp_iso = expires.isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Ensure user exists
        await db.execute("""
            INSERT INTO users (telegram_id, created_at, last_active_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET last_active_at = excluded.last_active_at
        """, (user_id, now_iso, now_iso))

        # Retire any previous active mailbox safely
        await db.execute("UPDATE mailboxes SET status = 'replaced' WHERE user_id = ? AND status = 'active'", (user_id,))

        # Insert new mailbox with EXACT matching column order
        cursor = await db.execute("""
            INSERT INTO mailboxes (user_id, address, provider, account_id, token, created_at, expires_at, status, extension_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0)
        """, (user_id, address, provider, account_id, token, now_iso, exp_iso))
        box_id = cursor.lastrowid

        # Record in rate limits
        await db.execute("INSERT INTO rate_limits (user_id, action, created_at) VALUES (?, 'create_mailbox', ?)",
                         (user_id, now_iso))

        await db.commit()

        async with db.execute("SELECT * FROM mailboxes WHERE id = ?", (box_id,)) as cur:
            return dict(await cur.fetchone())

async def extend_mailbox(user_id: int) -> tuple[bool, str, int]:
    now = datetime.now(timezone.utc)
    box = await get_active_mailbox(user_id)
    if not box:
        return False, "expired", 0

    if box["extension_count"] >= MAX_EXTENSIONS_PER_MAILBOX:
        return False, "max_extensions_reached", 0

    created_dt = datetime.fromisoformat(box["created_at"])
    curr_exp = datetime.fromisoformat(box["expires_at"])
    max_allowed_exp = created_dt + timedelta(minutes=MAX_TOTAL_LIFETIME_MINUTES)

    base = max(curr_exp, now)
    proposed_exp = base + timedelta(minutes=EXTENSION_INCREMENT_MINUTES)
    new_exp = min(proposed_exp, max_allowed_exp)

    if new_exp <= curr_exp:
        return False, "max_lifetime_reached", 0

    remaining_mins = max(1, int((new_exp - now).total_seconds() // 60))

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE mailboxes 
            SET expires_at = ?, extension_count = extension_count + 1 
            WHERE id = ?
        """, (new_exp.isoformat(), box["id"]))
        await db.commit()

    return True, new_exp.astimezone(IST).strftime("%I:%M %p IST"), remaining_mins

async def delete_mailbox(user_id: int) -> dict | None:
    box = await get_active_mailbox(user_id)
    if not box:
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE mailboxes SET status = 'deleted' WHERE id = ?", (box["id"],))
        await db.commit()

    return box

async def is_message_recorded(mailbox_id: int, provider_message_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM messages WHERE mailbox_id = ? AND provider_message_id = ?",
            (mailbox_id, str(provider_message_id))
        ) as cursor:
            return (await cursor.fetchone()) is not None

async def save_incoming_message(
    mailbox_id: int, provider_message_id: str, sender_address: str, sender_name: str,
    subject: str, received_at: str, otp_code: str | None, body_text: str
) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT OR IGNORE INTO messages 
            (mailbox_id, provider_message_id, sender_address, sender_name, subject, received_at, notified_at, otp_code, body_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (mailbox_id, str(provider_message_id), sender_address, sender_name, subject, received_at, now_iso, otp_code, body_text))
        await db.commit()
        return cursor.lastrowid

async def get_mailbox_messages(mailbox_id: int, page: int = 0, page_size: int = INBOX_PAGE_SIZE) -> tuple[list[dict], int]:
    offset = page * page_size
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) FROM messages WHERE mailbox_id = ?", (mailbox_id,)) as cur:
            total = (await cur.fetchone())[0]

        async with db.execute("""
            SELECT id, sender_name, sender_address, subject, received_at, otp_code, is_read
            FROM messages 
            WHERE mailbox_id = ? 
            ORDER BY id DESC 
            LIMIT ? OFFSET ?
        """, (mailbox_id, page_size, offset)) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows], total

async def get_unread_count(mailbox_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE mailbox_id = ? AND is_read = 0",
            (mailbox_id,)
        ) as cursor:
            return (await cursor.fetchone())[0]

async def get_message_detail(local_id: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT m.*, b.user_id, b.address as mailbox_address, b.provider, b.token as mailbox_token
            FROM messages m
            JOIN mailboxes b ON m.mailbox_id = b.id
            WHERE m.id = ? AND b.user_id = ?
        """
        async with db.execute(query, (local_id, user_id)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            await db.execute("UPDATE messages SET is_read = 1 WHERE id = ?", (local_id,))
            await db.commit()
            return dict(row)

async def get_adjacent_message_ids(local_id: int, mailbox_id: int) -> tuple[int | None, int | None]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM messages WHERE mailbox_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
            (mailbox_id, local_id)
        ) as cur:
            prev_row = await cur.fetchone()
            prev_id = prev_row[0] if prev_row else None

        async with db.execute(
            "SELECT id FROM messages WHERE mailbox_id = ? AND id > ? ORDER BY id ASC LIMIT 1",
            (mailbox_id, local_id)
        ) as cur:
            next_row = await cur.fetchone()
            next_id = next_row[0] if next_row else None

        return prev_id, next_id

async def delete_message(local_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT m.id FROM messages m
            JOIN mailboxes b ON m.mailbox_id = b.id
            WHERE m.id = ? AND b.user_id = ?
        """, (local_id, user_id)) as cur:
            if not await cur.fetchone():
                return False

        await db.execute("DELETE FROM messages WHERE id = ?", (local_id,))
        await db.commit()
        return True

async def get_mailboxes_for_monitoring() -> list[dict]:
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT b.id, b.user_id, b.address, b.provider, b.token, b.created_at, b.expires_at, 
                   b.last_polled_at, u.last_active_at
            FROM mailboxes b
            JOIN users u ON b.user_id = u.telegram_id
            WHERE b.status = 'active' AND b.expires_at > ?
        """
        async with db.execute(query, (now_iso,)) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def update_mailbox_polled_time(mailbox_id: int):
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE mailboxes SET last_polled_at = ? WHERE id = ?", (now_iso, mailbox_id))
        await db.commit()

async def get_system_counts() -> tuple[int, int]:
    """Returns (active_mailboxes_count, total_messages_count)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM mailboxes WHERE status = 'active' AND expires_at > ?", (now_iso,)) as cur:
            active_boxes = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM messages") as cur:
            total_msgs = (await cur.fetchone())[0]
        return active_boxes, total_msgs

async def get_bot_statistics() -> dict:
    """Returns comprehensive non-technical user & delivery statistics for owner."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Total unique users
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total_users = (await cur.fetchone())[0]

        # Total mailboxes created
        async with db.execute("SELECT COUNT(*) FROM mailboxes") as cur:
            total_mailboxes = (await cur.fetchone())[0]

        # Active mailboxes right now
        async with db.execute("SELECT COUNT(*) FROM mailboxes WHERE status = 'active' AND expires_at > ?", (now_iso,)) as cur:
            active_mailboxes = (await cur.fetchone())[0]

        # Total emails received
        async with db.execute("SELECT COUNT(*) FROM messages") as cur:
            total_messages = (await cur.fetchone())[0]

        # Verification codes captured
        async with db.execute("SELECT COUNT(*) FROM messages WHERE otp_code IS NOT NULL AND otp_code != ''") as cur:
            total_otps = (await cur.fetchone())[0]

        return {
            "total_users": total_users,
            "total_mailboxes": total_mailboxes,
            "active_mailboxes": active_mailboxes,
            "total_messages": total_messages,
            "total_otps": total_otps
        }
