from __future__ import annotations

import datetime as dt
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from app.models import ChatTurn

DEFAULT_CHAT_ID = "default"


class ChatStore:
    def __init__(self, db_path: str, default_user_id: str, default_user_name: str) -> None:
        self.db_path = db_path
        self.default_user_id = default_user_id
        self.default_user_name = default_user_name
        db_parent = Path(db_path).expanduser().resolve().parent
        db_parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        self._ensure_default_user()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return dt.datetime.now(dt.UTC).isoformat()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
            chat_columns = conn.execute("PRAGMA table_info(chats)").fetchall()
            chat_column_names = {row["name"] for row in chat_columns}
            if "user_id" not in chat_column_names:
                # Migration for previous schema that had no user ownership.
                conn.execute("ALTER TABLE chats ADD COLUMN user_id TEXT")
                conn.execute(
                    "UPDATE chats SET user_id = ? WHERE user_id IS NULL OR user_id = ''",
                    (self.default_user_id,),
                )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chats_user_updated ON chats(user_id, updated_at)"
            )

    def _ensure_default_user(self) -> None:
        with self._lock, self._connect() as conn:
            now = self._now()
            existing = conn.execute(
                "SELECT id FROM users WHERE id = ?",
                (self.default_user_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE users SET name = ?, updated_at = ? WHERE id = ?",
                    (self.default_user_name, now, self.default_user_id),
                )
                return
            conn.execute(
                "INSERT INTO users(id, name, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (self.default_user_id, self.default_user_name, now, now),
            )

    def get_user(self, user_id: str) -> dict[str, str] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id, name FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            return {"id": row["id"], "name": row["name"]}

    def list_chats(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    c.id,
                    c.title,
                    c.updated_at,
                    COUNT(m.id) as message_count
                FROM chats c
                LEFT JOIN messages m ON c.id = m.chat_id
                WHERE c.user_id = ?
                GROUP BY c.id, c.title, c.updated_at
                ORDER BY c.updated_at DESC
                """
                ,
                (user_id,),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "message_count": int(row["message_count"]),
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]

    def create_chat(self, user_id: str, title: str | None = None) -> dict[str, Any]:
        chat_id = str(uuid.uuid4())
        now = self._now()
        chat_title = (title or "New chat").strip() or "New chat"
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO chats(id, user_id, title, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                (chat_id, user_id, chat_title, now, now),
            )
        return {
            "id": chat_id,
            "title": chat_title,
            "message_count": 0,
            "updated_at": now,
        }

    def get_chat_summary(self, user_id: str, chat_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    c.id,
                    c.title,
                    c.updated_at,
                    COUNT(m.id) as message_count
                FROM chats c
                LEFT JOIN messages m ON c.id = m.chat_id
                WHERE c.id = ? AND c.user_id = ?
                GROUP BY c.id, c.title, c.updated_at
                """,
                (chat_id, user_id),
            ).fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "title": row["title"],
                "message_count": int(row["message_count"]),
                "updated_at": row["updated_at"],
            }

    def get_messages(self, user_id: str, chat_id: str) -> list[ChatTurn]:
        with self._lock, self._connect() as conn:
            owner = conn.execute(
                "SELECT id FROM chats WHERE id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            if owner is None:
                return []
            rows = conn.execute(
                "SELECT role, text FROM messages WHERE chat_id = ? ORDER BY id ASC",
                (chat_id,),
            ).fetchall()
            return [{"role": row["role"], "text": row["text"]} for row in rows]

    def get_or_create_chat(self, user_id: str, chat_id: str) -> dict[str, Any]:
        existing = self.get_chat_summary(user_id, chat_id)
        if existing:
            return existing
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO chats(id, user_id, title, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                (chat_id, user_id, "New chat", now, now),
            )
        return {
            "id": chat_id,
            "title": "New chat",
            "message_count": 0,
            "updated_at": now,
        }

    def append_message(self, user_id: str, chat_id: str, role: str, text: str) -> None:
        now = self._now()
        with self._lock, self._connect() as conn:
            owner = conn.execute(
                "SELECT id FROM chats WHERE id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            if owner is None:
                raise ValueError("chat not found")
            conn.execute(
                "INSERT INTO messages(chat_id, role, text, created_at) VALUES(?, ?, ?, ?)",
                (chat_id, role, text, now),
            )
            conn.execute(
                "UPDATE chats SET updated_at = ? WHERE id = ?",
                (now, chat_id),
            )

    def rename_chat(self, user_id: str, chat_id: str, title: str) -> bool:
        clean_title = title.strip()
        if not clean_title:
            return False
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM chats WHERE id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            if not existing:
                return False
            conn.execute(
                "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
                (clean_title, self._now(), chat_id),
            )
            return True

    def clear_chat(self, user_id: str, chat_id: str) -> bool:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM chats WHERE id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            if not existing:
                return False
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            conn.execute(
                "UPDATE chats SET updated_at = ? WHERE id = ?",
                (self._now(), chat_id),
            )
            return True

    def delete_chat(self, user_id: str, chat_id: str) -> bool:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM chats WHERE id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            if not existing:
                return False
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            return True
