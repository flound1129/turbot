"""SQLite persistence for feature request sessions and cooldowns."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cog_feature import ThreadSession

PROJECT_DIR: str = os.path.dirname(os.path.abspath(__file__))
DB_PATH: str = os.path.join(PROJECT_DIR, "data", "sessions.db")


def _connect() -> sqlite3.Connection:
    """Open a connection to the sessions database."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    """Create tables if they don't exist."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                thread_id   INTEGER PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                request_type TEXT NOT NULL,
                original_description TEXT NOT NULL,
                messages    TEXT NOT NULL,
                state       TEXT NOT NULL,
                refined_description TEXT,
                created_at  REAL NOT NULL,
                last_active REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cooldowns (
                user_id      INTEGER PRIMARY KEY,
                last_request REAL NOT NULL
            )
            """
        )


def save_session(session: ThreadSession) -> None:
    """Insert or update a session row."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions
                (thread_id, user_id, request_type, original_description,
                 messages, state, refined_description, created_at, last_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.thread_id,
                session.user_id,
                session.request_type,
                session.original_description,
                json.dumps(session.messages, ensure_ascii=False),
                session.state,
                session.refined_description,
                session.created_at,
                session.last_active,
            ),
        )


def load_active_sessions() -> list[dict]:
    """Load all sessions not in 'done' state, returned as dicts."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM sessions WHERE state != 'done'"
        ).fetchall()
    return [
        {
            "thread_id": row["thread_id"],
            "user_id": row["user_id"],
            "request_type": row["request_type"],
            "original_description": row["original_description"],
            "messages": json.loads(row["messages"]),
            "state": row["state"],
            "refined_description": row["refined_description"],
            "created_at": row["created_at"],
            "last_active": row["last_active"],
        }
        for row in rows
    ]


def delete_session(thread_id: int) -> None:
    """Remove a session row."""
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE thread_id = ?", (thread_id,))


def save_cooldown(user_id: int, timestamp: float) -> None:
    """Insert or update a cooldown entry."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cooldowns (user_id, last_request) VALUES (?, ?)",
            (user_id, timestamp),
        )


def load_cooldowns() -> dict[int, float]:
    """Load all cooldown entries as {user_id: timestamp}."""
    with _connect() as conn:
        rows = conn.execute("SELECT user_id, last_request FROM cooldowns").fetchall()
    return {row[0]: row[1] for row in rows}


def delete_expired_cooldowns(cutoff: float) -> None:
    """Remove cooldown entries older than cutoff (Unix epoch)."""
    with _connect() as conn:
        conn.execute("DELETE FROM cooldowns WHERE last_request < ?", (cutoff,))
