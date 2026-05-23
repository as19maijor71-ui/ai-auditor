import json
import logging
import sqlite3
from typing import Optional

from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from auditor.config import settings

logger = logging.getLogger(__name__)


class SQLiteStorage(BaseStorage):
    def __init__(self, db_path: str, fsm_ttl: int = 86400, admin_id: int = 0) -> None:
        self.db_path = db_path
        self.fsm_ttl = fsm_ttl
        self.admin_id = admin_id
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _key_str(self, key: StorageKey) -> str:
        thread = f":{key.thread_id}" if key.thread_id is not None else ""
        return f"{key.chat_id}:{key.user_id}{thread}"

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fsm_states ("
            "key TEXT PRIMARY KEY, "
            "state TEXT, "
            "data TEXT, "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS audit_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER NOT NULL, "
            "username TEXT, "
            "url TEXT, "
            "platform TEXT, "
            "score INTEGER, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS whitelist ("
            "user_id INTEGER PRIMARY KEY, "
            "username TEXT, "
            "full_name TEXT DEFAULT '', "
            "approved_by INTEGER, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        try:
            conn.execute("ALTER TABLE whitelist ADD COLUMN full_name TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            f"DELETE FROM fsm_states WHERE updated_at < datetime('now', '-{self.fsm_ttl} seconds')"
        )
        if self.admin_id:
            conn.execute(
                "INSERT OR IGNORE INTO whitelist (user_id, username, full_name, approved_by) "
                "VALUES (?, ?, ?, ?)",
                (self.admin_id, "admin", "Организатор", self.admin_id),
            )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS copy_cache ("
            "key TEXT PRIMARY KEY, "
            "text TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_usage ("
            "user_id INTEGER PRIMARY KEY, "
            "free_audits_used INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.commit()
        logger.info("SQLiteStorage initialized at %s", self.db_path)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        key_str = self._key_str(key)
        conn = self._get_conn()
        if state is None:
            state_val = None
        elif isinstance(state, str):
            state_val = state
        else:
            state_val = state.state if hasattr(state, "state") else str(state)
        existing = conn.execute(
            "SELECT data FROM fsm_states WHERE key = ?", (key_str,)
        ).fetchone()
        existing_data = existing["data"] if existing else None
        conn.execute(
            "INSERT OR REPLACE INTO fsm_states (key, state, data, updated_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (key_str, state_val, existing_data),
        )
        conn.commit()

    async def get_state(self, key: StorageKey) -> Optional[str]:
        key_str = self._key_str(key)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT state FROM fsm_states WHERE key = ?", (key_str,)
        ).fetchone()
        if row and row["state"]:
            return row["state"]
        return None

    async def set_data(self, key: StorageKey, data: dict) -> None:
        key_str = self._key_str(key)
        conn = self._get_conn()
        data_json = json.dumps(data, default=str)
        existing = conn.execute(
            "SELECT state FROM fsm_states WHERE key = ?", (key_str,)
        ).fetchone()
        existing_state = existing["state"] if existing else None
        conn.execute(
            "INSERT OR REPLACE INTO fsm_states (key, state, data, updated_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (key_str, existing_state, data_json),
        )
        conn.commit()

    async def get_data(self, key: StorageKey) -> dict:
        key_str = self._key_str(key)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT data FROM fsm_states WHERE key = ?", (key_str,)
        ).fetchone()
        if row and row["data"]:
            return json.loads(row["data"])
        return {}

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.info("SQLiteStorage connection closed")

    def log_audit(self, user_id: int, username: str | None, url: str, platform: str, score: int) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO audit_log (user_id, username, url, platform, score) VALUES (?, ?, ?, ?, ?)",
            (user_id, username or "", url, platform, score),
        )
        conn.commit()

    def get_recent_activity(self, limit: int = 20) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT user_id, username, url, platform, score, created_at "
            "FROM audit_log "
            "WHERE created_at > datetime('now', '-7 days') "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def is_whitelisted(self, user_id: int) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None

    def add_to_whitelist(self, user_id: int, username: str, full_name: str, approved_by: int) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO whitelist (user_id, username, full_name, approved_by) "
            "VALUES (?, ?, ?, ?)",
            (user_id, username, full_name, approved_by),
        )
        conn.commit()

    def remove_from_whitelist(self, user_id: int) -> None:
        conn = self._get_conn()
        conn.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
        conn.commit()

    def get_whitelist_users(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT user_id, username, full_name, created_at FROM whitelist ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]

    def store_copy_data(self, key: str, text: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS copy_cache ("
            "key TEXT PRIMARY KEY, "
            "text TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.execute(
            "INSERT OR REPLACE INTO copy_cache (key, text, created_at) VALUES (?, ?, datetime('now'))",
            (key, text),
        )
        conn.commit()

    def get_copy_data(self, key: str) -> str | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT text FROM copy_cache WHERE key = ?", (key,)
        ).fetchone()
        return row["text"] if row else None

    def get_usage(self, user_id: int) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT free_audits_used FROM user_usage WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["free_audits_used"] if row else 0

    def increment_usage(self, user_id: int) -> int:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO user_usage (user_id, free_audits_used, updated_at) "
            "VALUES (?, 1, datetime('now')) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "free_audits_used = free_audits_used + 1, "
            "updated_at = datetime('now')",
            (user_id,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT free_audits_used FROM user_usage WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["free_audits_used"] if row else 0

    def remaining_free_audits(self, user_id: int) -> int:
        used = self.get_usage(user_id)
        return max(0, settings.FREE_AUDIT_LIMIT - used)

    def has_free_audits(self, user_id: int) -> bool:
        return self.get_usage(user_id) < settings.FREE_AUDIT_LIMIT
