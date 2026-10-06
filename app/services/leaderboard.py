"""Leaderboard, bans and room-history service.

All functions swallow ``sqlite3`` errors and log them, returning safe defaults so
that a database hiccup never takes down a live game.  Writes use
``INSERT ... ON CONFLICT`` upserts to stay atomic under concurrency.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable

from ..db import leaderboard_connection
from ..logging_setup import get_logger

logger = get_logger(__name__)

BAN_KIND_NAME = "name"
BAN_KIND_IP = "ip"
HISTORY_RETENTION_DAYS = 30


# ─────────────────────────────────────────────────────────────
#  Bans
# ─────────────────────────────────────────────────────────────
def ban(identifier: str, reason: str = "", minutes: int = 60, *, kind: str = BAN_KIND_NAME) -> bool:
    """Ban ``identifier`` until ``now + minutes``."""
    identifier = (identifier or "").strip().lower()
    if not identifier:
        return False
    expires_at = int(time.time()) + max(1, int(minutes)) * 60
    try:
        with leaderboard_connection() as conn:
            conn.execute(
                "INSERT INTO bans(identifier, reason, expires_at, kind) VALUES(?,?,?,?) "
                "ON CONFLICT(identifier) DO UPDATE SET reason=excluded.reason, "
                "expires_at=excluded.expires_at, kind=excluded.kind",
                (identifier, reason[:200], expires_at, kind),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("ban(%s): %s", identifier, exc)
        return False


def unban(identifier: str) -> bool:
    """Remove a ban by identifier."""
    try:
        with leaderboard_connection() as conn:
            conn.execute("DELETE FROM bans WHERE identifier=?", ((identifier or "").strip().lower(),))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("unban(%s): %s", identifier, exc)
        return False


def is_banned(identifier: str, *, kind: str | None = None) -> bool:
    """Return ``True`` when ``identifier`` has an active ban."""
    identifier = (identifier or "").strip().lower()
    if not identifier:
        return False
    now = int(time.time())
    try:
        with leaderboard_connection() as conn:
            if kind:
                row = conn.execute(
                    "SELECT expires_at FROM bans WHERE identifier=? AND kind=?",
                    (identifier, kind),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT expires_at FROM bans WHERE identifier=?", (identifier,)
                ).fetchone()
            if row is None:
                return False
            if row["expires_at"] > now:
                return True
            conn.execute("DELETE FROM bans WHERE identifier=?", (identifier,))
            return False
    except Exception as exc:  # noqa: BLE001
        logger.error("is_banned(%s): %s", identifier, exc)
        return False


def list_bans(kind: str | None = None) -> list[dict]:
    """Return all currently active bans."""
    now = int(time.time())
    try:
        with leaderboard_connection() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM bans WHERE expires_at>? AND kind=?", (now, kind)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM bans WHERE expires_at>?", (now,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("list_bans: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────
#  Room history
# ─────────────────────────────────────────────────────────────
def save_room_history(code: str, duration: int, mode: str, topic: str,
                      players: list[dict], questions: list[dict]) -> None:
    """Persist a finished game for later inspection by administrators."""
    try:
        with leaderboard_connection() as conn:
            conn.execute(
                "INSERT INTO room_history(room_code, played_at, duration, mode, topic, "
                "players_json, questions_json) VALUES(?,?,?,?,?,?,?)",
                (
                    code.upper(), int(time.time()), int(duration), mode, topic,
                    json.dumps(players, ensure_ascii=False),
                    json.dumps(questions, ensure_ascii=False),
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("save_room_history(%s): %s", code, exc)


def get_room_history(code: str, limit: int = 10) -> list[dict]:
    """Return the most recent games for a room code."""
    try:
        with leaderboard_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM room_history WHERE room_code=? ORDER BY played_at DESC LIMIT ?",
                (code.upper(), limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["players"] = json.loads(item.pop("players_json", "[]") or "[]")
            item["questions"] = json.loads(item.pop("questions_json", "[]") or "[]")
            result.append(item)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("get_room_history(%s): %s", code, exc)
        return []


# ─────────────────────────────────────────────────────────────
#  Leaderboard
# ─────────────────────────────────────────────────────────────
def update_leaderboard(players: Iterable[dict], duration: int = 0) -> None:
    """Apply a finished game's scores to the all-time leaderboard."""
    players = list(players or [])
    if not players:
        return
    best = max((int(p.get("score", 0)) for p in players), default=0)
    now = int(time.time())
    try:
        with leaderboard_connection() as conn:
            for player in players:
                name = str(player.get("name", ""))[:64]
                if not name:
                    continue
                score = int(player.get("score", 0))
                won = 1 if score == best and score > 0 else 0
                conn.execute(
                    """
                    INSERT INTO leaderboard(username, total_score, games_played, wins, last_seen, total_time)
                    VALUES(?,?,1,?,?,?)
                    ON CONFLICT(username) DO UPDATE SET
                        total_score = total_score + excluded.total_score,
                        games_played = games_played + 1,
                        wins = wins + excluded.wins,
                        last_seen = excluded.last_seen,
                        total_time = total_time + excluded.total_time
                    """,
                    (name, score, won, now, int(duration)),
                )
    except Exception as exc:  # noqa: BLE001
        logger.error("update_leaderboard: %s", exc)


def top_players(limit: int = 50) -> list[dict]:
    """Return the global leaderboard ordered by total score."""
    try:
        with leaderboard_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM leaderboard ORDER BY total_score DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("top_players: %s", exc)
        return []


def all_players(nick: str = "", limit: int = 200) -> list[dict]:
    """Return leaderboard rows, optionally filtered by nickname substring."""
    try:
        with leaderboard_connection() as conn:
            if nick:
                rows = conn.execute(
                    "SELECT * FROM leaderboard WHERE username LIKE ? ORDER BY total_score DESC LIMIT ?",
                    (f"%{nick}%", int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM leaderboard ORDER BY total_score DESC LIMIT ?", (int(limit),)
                ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("all_players: %s", exc)
        return []


def player_rank(username: str) -> dict:
    """Return a player's leaderboard row including their rank."""
    try:
        with leaderboard_connection() as conn:
            row = conn.execute(
                "SELECT *, (SELECT COUNT(*)+1 FROM leaderboard l2 WHERE l2.total_score > l.total_score) AS rank "
                "FROM leaderboard l WHERE username=?",
                (username,),
            ).fetchone()
        return dict(row) if row else {}
    except Exception as exc:  # noqa: BLE001
        logger.error("player_rank(%s): %s", username, exc)
        return {}


def reset_player_stats(username: str) -> bool:
    """Zero a single player's aggregate statistics."""
    try:
        with leaderboard_connection() as conn:
            conn.execute(
                "UPDATE leaderboard SET total_score=0, wins=0, games_played=0, total_time=0 WHERE username=?",
                (username,),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("reset_player_stats(%s): %s", username, exc)
        return False


def reset_server_stats() -> dict[str, int]:
    """Purge old history and expired bans.  Returns removed row counts."""
    now = int(time.time())
    cutoff = now - HISTORY_RETENTION_DAYS * 86400
    try:
        with leaderboard_connection() as conn:
            history = conn.execute("DELETE FROM room_history WHERE played_at<?", (cutoff,)).rowcount
            bans = conn.execute("DELETE FROM bans WHERE expires_at<?", (now,)).rowcount
        return {"history_removed": max(0, history), "bans_removed": max(0, bans)}
    except Exception as exc:  # noqa: BLE001
        logger.error("reset_server_stats: %s", exc)
        return {"history_removed": 0, "bans_removed": 0}
