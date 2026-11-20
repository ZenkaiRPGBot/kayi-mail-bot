import aiosqlite
import os
from datetime import datetime, timezone, timedelta
from config import MAILBOX_LIFETIME_MINUTES

DB_PATH = os.path.join(os.path.dirname(__file__), "kayimail.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                email TEXT,
                token TEXT,
                account_id TEXT,
                created_at TIMESTAMP,
                expires_at TIMESTAMP,
                last_checked_msg_id TEXT DEFAULT '',
                last_generated_at TIMESTAMP,
                last_active_at TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS received_messages (
                msg_id TEXT PRIMARY KEY,
                user_id INTEGER,
                sender TEXT,
                subject TEXT,
                otp_code TEXT,
                received_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        """)
        await db.commit()

async def get_user_mailbox(user_id: int):
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            
            # Check if expired
            if row["expires_at"]:
                exp_dt = datetime.fromisoformat(row["expires_at"])
                if exp_dt < now:
                    # Mailbox expired
                    await db.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
                    await db.commit()
                    return None
            return dict(row)

async def can_generate_new(user_id: int, cooldown_seconds: int = 60) -> tuple[bool, int]:
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT last_generated_at FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row or not row[0]:
                return True, 0
            
            last_gen = datetime.fromisoformat(row[0])
            elapsed = (now - last_gen).total_seconds()
            if elapsed < cooldown_seconds:
                remaining = int(cooldown_seconds - elapsed)
                return False, remaining
            return True, 0

async def save_user_mailbox(user_id: int, email: str, token: str, account_id: str):
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=MAILBOX_LIFETIME_MINUTES)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, email, token, account_id, created_at, expires_at, last_generated_at, last_active_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                email = excluded.email,
                token = excluded.token,
                account_id = excluded.account_id,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at,
                last_generated_at = excluded.last_generated_at,
                last_active_at = excluded.last_active_at,
                last_checked_msg_id = '',
                is_active = 1
        """, (user_id, email, token, account_id, now.isoformat(), expires.isoformat(), now.isoformat(), now.isoformat()))
        await db.commit()

async def extend_mailbox(user_id: int, extra_minutes: int = 30) -> bool:
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT expires_at FROM users WHERE user_id = ? AND is_active = 1", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row or not row["expires_at"]:
                return False
            
            current_exp = datetime.fromisoformat(row["expires_at"])
            base = max(current_exp, now)
            new_exp = base + timedelta(minutes=extra_minutes)
            
            await db.execute("UPDATE users SET expires_at = ?, last_active_at = ? WHERE user_id = ?",
                             (new_exp.isoformat(), now.isoformat(), user_id))
            await db.commit()
            return True

async def delete_user_mailbox(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_active = 0, email = NULL, token = NULL, account_id = NULL WHERE user_id = ?", (user_id,))
        await db.commit()

async def touch_user_activity(user_id: int):
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_active_at = ? WHERE user_id = ?", (now.isoformat(), user_id))
        await db.commit()

async def get_active_mailboxes():
    """Returns all non-expired, active mailboxes that were active within the last 15 minutes."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=15)
    
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT * FROM users 
            WHERE is_active = 1 
              AND email IS NOT NULL 
              AND expires_at > ? 
              AND last_active_at > ?
        """
        async with db.execute(query, (now.isoformat(), cutoff.isoformat())) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

async def update_last_checked_msg(user_id: int, msg_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_checked_msg_id = ? WHERE user_id = ?", (msg_id, user_id))
        await db.commit()

async def is_message_recorded(msg_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM received_messages WHERE msg_id = ?", (msg_id,)) as cursor:
            return (await cursor.fetchone()) is not None

async def record_message(msg_id: str, user_id: int, sender: str, subject: str, otp_code: str | None):
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO received_messages (msg_id, user_id, sender, subject, otp_code, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (msg_id, user_id, sender, subject, otp_code, now.isoformat()))
        await db.commit()
