"""Tests for the SQLite session persistence layer."""

import sqlite3
import time

import pytest

import session_store
from cog_feature import ThreadSession


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    """Point session_store at a temporary database for each test."""
    db_path = str(tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    session_store.init_db()


def _make_session(**overrides) -> ThreadSession:
    defaults = {
        "thread_id": 1000,
        "user_id": 2000,
        "request_type": "plugin",
        "original_description": "add a leaderboard",
        "messages": [{"role": "user", "content": "Feature request: add a leaderboard"}],
        "state": "discussing",
        "created_at": time.time(),
        "last_active": time.time(),
        "refined_description": None,
        "branch_name": None,
        "pr_url": None,
        "steps": [],
    }
    defaults.update(overrides)
    return ThreadSession(**defaults)


class TestInitDb:
    def test_creates_tables(self, tmp_path) -> None:
        db_path = str(tmp_path / "sessions.db")
        conn = sqlite3.connect(db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert "sessions" in table_names
        assert "cooldowns" in table_names
        conn.close()

    def test_idempotent(self) -> None:
        session_store.init_db()
        session_store.init_db()


class TestSaveAndLoadSession:
    def test_round_trip(self) -> None:
        session = _make_session()
        session_store.save_session(session)

        rows = session_store.load_active_sessions()
        assert len(rows) == 1
        row = rows[0]
        assert row["thread_id"] == 1000
        assert row["user_id"] == 2000
        assert row["request_type"] == "plugin"
        assert row["original_description"] == "add a leaderboard"
        assert row["messages"] == [{"role": "user", "content": "Feature request: add a leaderboard"}]
        assert row["state"] == "discussing"
        assert row["refined_description"] is None

    def test_upsert_updates_existing(self) -> None:
        session = _make_session()
        session_store.save_session(session)

        session.state = "plan_ready"
        session.refined_description = "Updated plan"
        session_store.save_session(session)

        rows = session_store.load_active_sessions()
        assert len(rows) == 1
        assert rows[0]["state"] == "plan_ready"
        assert rows[0]["refined_description"] == "Updated plan"

    def test_multiple_sessions(self) -> None:
        session1 = _make_session(thread_id=1000)
        session2 = _make_session(thread_id=2000)
        session_store.save_session(session1)
        session_store.save_session(session2)

        rows = session_store.load_active_sessions()
        assert len(rows) == 2


class TestDeleteSession:
    def test_removes_session(self) -> None:
        session = _make_session()
        session_store.save_session(session)
        session_store.delete_session(1000)

        rows = session_store.load_active_sessions()
        assert len(rows) == 0

    def test_delete_nonexistent_is_noop(self) -> None:
        session_store.delete_session(9999)


class TestLoadActiveSessions:
    def test_excludes_done(self) -> None:
        active = _make_session(thread_id=1000, state="discussing")
        done = _make_session(thread_id=2000, state="done")
        session_store.save_session(active)
        session_store.save_session(done)

        rows = session_store.load_active_sessions()
        assert len(rows) == 1
        assert rows[0]["thread_id"] == 1000

    def test_includes_plan_ready_and_generating(self) -> None:
        s1 = _make_session(thread_id=1000, state="plan_ready")
        s2 = _make_session(thread_id=2000, state="generating")
        session_store.save_session(s1)
        session_store.save_session(s2)

        rows = session_store.load_active_sessions()
        assert len(rows) == 2


class TestCooldowns:
    def test_save_and_load(self) -> None:
        now = time.time()
        session_store.save_cooldown(111, now)
        session_store.save_cooldown(222, now - 60)

        cooldowns = session_store.load_cooldowns()
        assert cooldowns[111] == pytest.approx(now)
        assert cooldowns[222] == pytest.approx(now - 60)

    def test_upsert_cooldown(self) -> None:
        session_store.save_cooldown(111, 1000.0)
        session_store.save_cooldown(111, 2000.0)

        cooldowns = session_store.load_cooldowns()
        assert cooldowns[111] == 2000.0

    def test_delete_expired(self) -> None:
        now = time.time()
        session_store.save_cooldown(111, now)        # fresh
        session_store.save_cooldown(222, now - 9999)  # expired

        session_store.delete_expired_cooldowns(now - 500)

        cooldowns = session_store.load_cooldowns()
        assert 111 in cooldowns
        assert 222 not in cooldowns


class TestStepTracking:
    def test_round_trip_with_steps(self) -> None:
        steps = [
            {
                "name": "code_generation",
                "status": "completed",
                "started_at": 1707900000.0,
                "completed_at": 1707900005.0,
                "error": None,
                "detail": "2 file(s) changed",
            },
            {
                "name": "create_branch",
                "status": "completed",
                "started_at": 1707900005.0,
                "completed_at": 1707900006.0,
                "error": None,
                "detail": "feature/leaderboard",
            },
        ]
        session = _make_session(
            branch_name="feature/leaderboard",
            pr_url="https://github.com/user/repo/pull/42",
            steps=steps,
        )
        session_store.save_session(session)

        rows = session_store.load_active_sessions()
        assert len(rows) == 1
        row = rows[0]
        assert row["branch_name"] == "feature/leaderboard"
        assert row["pr_url"] == "https://github.com/user/repo/pull/42"
        assert len(row["steps"]) == 2
        assert row["steps"][0]["name"] == "code_generation"
        assert row["steps"][1]["detail"] == "feature/leaderboard"

    def test_round_trip_with_null_steps(self) -> None:
        """Session with no steps round-trips as empty list."""
        session = _make_session()
        session_store.save_session(session)

        rows = session_store.load_active_sessions()
        assert len(rows) == 1
        assert rows[0]["branch_name"] is None
        assert rows[0]["pr_url"] is None
        assert rows[0]["steps"] == []

    def test_migration_adds_columns(self, tmp_path) -> None:
        """init_db adds new columns to an existing table without them."""
        db_path = str(tmp_path / "legacy.db")
        # Create legacy table without new columns
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE sessions (
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
            CREATE TABLE cooldowns (
                user_id      INTEGER PRIMARY KEY,
                last_request REAL NOT NULL
            )
            """
        )
        conn.close()

        # Point session_store at the legacy DB and run init_db
        import unittest.mock
        with unittest.mock.patch.object(session_store, "DB_PATH", db_path):
            session_store.init_db()

        # Verify new columns exist
        conn = sqlite3.connect(db_path)
        cols = [
            row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        ]
        conn.close()
        assert "branch_name" in cols
        assert "pr_url" in cols
        assert "steps" in cols
