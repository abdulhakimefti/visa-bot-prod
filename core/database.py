"""
core/database.py
SQLite async database for scan logs, user config, and found slots.
"""
import aiosqlite
import asyncio
import json
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "visa_bot.db"


async def init_db():
    """Create tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS scan_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at  TEXT NOT NULL,
                status      TEXT NOT NULL,       -- 'found' | 'empty' | 'error'
                slots_found INTEGER DEFAULT 0,
                message     TEXT
            );

            CREATE TABLE IF NOT EXISTS found_slots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                found_at     TEXT NOT NULL,
                slot_date    TEXT NOT NULL,
                slot_time    TEXT,
                location     TEXT,
                slots_count  INTEGER,
                booked       INTEGER DEFAULT 0,
                booking_ref  TEXT
            );

            CREATE TABLE IF NOT EXISTS user_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_actions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                action_type TEXT NOT NULL,      -- 'confirm_booking'
                payload     TEXT,               -- JSON
                status      TEXT DEFAULT 'pending'  -- 'pending' | 'confirmed' | 'rejected' | 'timeout'
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                username         TEXT NOT NULL,
                password         TEXT NOT NULL,
                security_answers TEXT DEFAULT '{}',
                date_from        TEXT DEFAULT '',
                date_to          TEXT DEFAULT '',
                created_at       TEXT NOT NULL
            );
        """)

        # Migrate older `accounts` tables created before per-account dates
        async with db.execute("PRAGMA table_info(accounts)") as cursor:
            existing_cols = [row[1] for row in await cursor.fetchall()]
        if "date_from" not in existing_cols:
            await db.execute("ALTER TABLE accounts ADD COLUMN date_from TEXT DEFAULT ''")
        if "date_to" not in existing_cols:
            await db.execute("ALTER TABLE accounts ADD COLUMN date_to TEXT DEFAULT ''")

        await db.commit()


async def log_scan(status: str, slots_found: int = 0, message: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO scan_logs (scanned_at, status, slots_found, message) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(), status, slots_found, message)
        )
        await db.commit()


async def save_slot(slot_date: str, slot_time: str, location: str, slots_count: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO found_slots (found_at, slot_date, slot_time, location, slots_count)
               VALUES (?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(), slot_date, slot_time, location, slots_count)
        )
        await db.commit()
        return cursor.lastrowid


async def mark_slot_booked(slot_id: int, booking_ref: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE found_slots SET booked=1, booking_ref=? WHERE id=?",
            (booking_ref, slot_id)
        )
        await db.commit()


async def create_pending_action(action_type: str, payload: dict) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO pending_actions (created_at, action_type, payload) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), action_type, json.dumps(payload))
        )
        await db.commit()
        return cursor.lastrowid


async def resolve_pending_action(action_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pending_actions SET status=? WHERE id=?",
            (status, action_id)
        )
        await db.commit()


async def get_pending_action(action_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pending_actions WHERE id=?", (action_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
    return None


async def get_scan_summary(last_n: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM scan_logs ORDER BY id DESC LIMIT ?", (last_n,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def set_config(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_config (key, value) VALUES (?, ?)",
            (key, value)
        )
        await db.commit()


async def get_config(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM user_config WHERE key=?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


# ── Account CRUD ────────────────────────────────────────────────────────── #

async def add_account(
    username: str,
    password: str,
    security_answers: dict,
    date_from: str = "",
    date_to: str = "",
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO accounts
                   (username, password, security_answers, date_from, date_to, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (username, password, json.dumps(security_answers),
             date_from, date_to, datetime.now().isoformat())
        )
        await db.commit()
        return cursor.lastrowid


async def update_account_dates(account_id: int, date_from: str, date_to: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET date_from=?, date_to=? WHERE id=?",
            (date_from, date_to, account_id)
        )
        await db.commit()
    return True


async def remove_account(account_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        await db.commit()
    return True


async def list_accounts() -> list[dict]:
    """Returns id, username, date range, created_at (no passwords)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, date_from, date_to, created_at FROM accounts ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_all_accounts() -> list[dict]:
    """Returns all fields including password and parsed security_answers."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM accounts ORDER BY id") as cursor:
            rows = await cursor.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try:
                    d["security_answers"] = json.loads(d["security_answers"])
                except Exception:
                    d["security_answers"] = {}
                result.append(d)
            return result


async def get_account(account_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                d = dict(row)
                try:
                    d["security_answers"] = json.loads(d["security_answers"])
                except Exception:
                    d["security_answers"] = {}
                return d
    return None
