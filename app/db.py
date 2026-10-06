"""SQLite access layer.

Two logical databases live under ``DATA_DIR``:

``leaderboard.db``
    Anonymous gameplay data: leaderboard, bans, room history.
``user_data.db``
    Registered accounts: users, UGC questions, campaign progress, achievements.

Both use WAL journaling.  Connections are short lived and created per request
because SQLite objects are not safe to share across eventlet green threads;
``check_same_thread=False`` plus the ``timeout`` pragma keeps concurrent writers
from raising ``database is locked``.

Schema management uses ``PRAGMA table_info`` introspection instead of
``ALTER TABLE ... except: pass`` so migrations are explicit and idempotent.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .config import settings

LEADERBOARD_DB = settings.data_dir / "leaderboard.db"
USER_DB = settings.data_dir / "user_data.db"

_SCHEMA_LEADERBOARD = """
CREATE TABLE IF NOT EXISTS leaderboard (
    username     TEXT PRIMARY KEY,
    total_score  INTEGER DEFAULT 0,
    games_played INTEGER DEFAULT 0,
    wins         INTEGER DEFAULT 0,
    last_seen    INTEGER DEFAULT 0,
    total_time   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bans (
    identifier TEXT PRIMARY KEY,
    reason     TEXT,
    expires_at INTEGER DEFAULT 0,
    kind       TEXT DEFAULT 'name'
);
CREATE TABLE IF NOT EXISTS room_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    room_code      TEXT NOT NULL,
    played_at      INTEGER NOT NULL,
    duration       INTEGER DEFAULT 0,
    mode           TEXT,
    topic          TEXT,
    players_json   TEXT,
    questions_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_lb_score ON leaderboard(total_score DESC);
CREATE INDEX IF NOT EXISTS idx_history_room ON room_history(room_code, played_at DESC);
CREATE INDEX IF NOT EXISTS idx_bans_expires ON bans(expires_at);
"""

_SCHEMA_USERS = """
CREATE TABLE IF NOT EXISTS users (
    username     TEXT PRIMARY KEY,
    pwd_hash     TEXT NOT NULL,
    xp           INTEGER DEFAULT 0,
    coins        INTEGER DEFAULT 50,
    games_played INTEGER DEFAULT 0,
    wins         INTEGER DEFAULT 0,
    total_score  INTEGER DEFAULT 0,
    created_at   INTEGER DEFAULT 0,
    last_seen    INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ugc_questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    author        TEXT NOT NULL,
    question      TEXT NOT NULL,
    options       TEXT NOT NULL,
    correct       INTEGER NOT NULL,
    topic         TEXT DEFAULT '',
    difficulty    INTEGER DEFAULT 2,
    status        TEXT DEFAULT 'pending',
    rating        REAL DEFAULT 0.0,
    usage_count   INTEGER DEFAULT 0,
    correct_pct   REAL DEFAULT 0.0,
    created_at    INTEGER DEFAULT 0,
    reject_reason TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ugc_votes (
    question_id INTEGER NOT NULL,
    voter       TEXT NOT NULL,
    vote        INTEGER NOT NULL,
    PRIMARY KEY (question_id, voter)
);
CREATE TABLE IF NOT EXISTS ugc_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    reporter    TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    created_at  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS campaign_progress (
    username     TEXT NOT NULL,
    level_id     INTEGER NOT NULL,
    stars        INTEGER DEFAULT 0,
    best_score   INTEGER DEFAULT 0,
    completed_at INTEGER DEFAULT 0,
    PRIMARY KEY (username, level_id)
);
CREATE TABLE IF NOT EXISTS achievements (
    username    TEXT NOT NULL,
    ach_id      TEXT NOT NULL,
    unlocked_at INTEGER DEFAULT 0,
    PRIMARY KEY (username, ach_id)
);
CREATE INDEX IF NOT EXISTS idx_ugc_status ON ugc_questions(status, rating DESC);
CREATE INDEX IF NOT EXISTS idx_ugc_topic  ON ugc_questions(topic, status);
CREATE INDEX IF NOT EXISTS idx_camp       ON campaign_progress(username);
"""

#: Additive migrations: table -> [(column, ddl_type_and_default), ...]
_MIGRATIONS: dict[str, list[tuple[str, str, str]]] = {
    str(LEADERBOARD_DB): [("bans", "kind", "TEXT DEFAULT 'name'")],
    str(USER_DB): [("users", "last_seen", "INTEGER DEFAULT 0")],
}


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-8000")
    return conn


@contextmanager
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a transactional connection, committing on success."""
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def leaderboard_connection() -> contextmanager:
    """Transactional connection to ``leaderboard.db``."""
    return connection(LEADERBOARD_DB)


def user_connection() -> contextmanager:
    """Transactional connection to ``user_data.db``."""
    return connection(USER_DB)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {row["name"] for row in rows}


def _apply_migrations(conn: sqlite3.Connection, path: Path) -> None:
    for table, column, ddl in _MIGRATIONS.get(str(path), []):
        if column not in _existing_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_databases() -> None:
    """Create every table/index and apply additive migrations."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with connection(LEADERBOARD_DB) as conn:
        conn.executescript(_SCHEMA_LEADERBOARD)
        _apply_migrations(conn, LEADERBOARD_DB)
    with connection(USER_DB) as conn:
        conn.executescript(_SCHEMA_USERS)
        _apply_migrations(conn, USER_DB)


def db_health() -> dict[str, object]:
    """Cheap liveness probe used by ``/health``."""
    result: dict[str, object] = {}
    for label, path in (("leaderboard", LEADERBOARD_DB), ("users", USER_DB)):
        try:
            with connection(path) as conn:
                conn.execute("SELECT 1").fetchone()
            result[label] = "ok"
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            result[label] = f"error: {exc}"
    return result


def table_names(path: Path) -> Sequence[str]:
    """Return user table names (used by tests)."""
    with connection(path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return [r["name"] for r in rows]


def is_healthy(path: Path) -> bool:
    """Return ``True`` when the database file is readable."""
    if not os.path.exists(path):
        return False
    try:
        with connection(path) as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except sqlite3.Error:
        return False
