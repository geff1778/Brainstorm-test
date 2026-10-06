"""Account service: registration, login, profile and currency.

Passwords are hashed with PBKDF2 (see :mod:`app.security`).  Legacy
``salt:sha256`` hashes are verified and transparently upgraded on next login.
"""

from __future__ import annotations

import math
import sqlite3
import time

from ..db import user_connection
from ..errors import AuthError, ConflictError, ValidationError
from ..logging_setup import get_logger
from ..security import hash_password, needs_rehash, verify_password
from ..validation import validate_password, validate_username

logger = get_logger(__name__)

STARTING_COINS = 50
XP_PER_LEVEL_FACTOR = 100


def register_user(username: str, password: str) -> dict:
    """Create an account and return its public representation.

    Raises
    ------
    ValidationError
        When the username or password is invalid.
    ConflictError
        When the username is already taken.
    """
    name = validate_username(username)
    secret = validate_password(password)
    try:
        with user_connection() as conn:
            conn.execute(
                "INSERT INTO users(username, pwd_hash, coins, created_at, last_seen) VALUES(?,?,?,?,?)",
                (name, hash_password(secret), STARTING_COINS, int(time.time()), int(time.time())),
            )
    except sqlite3.IntegrityError as exc:
        raise ConflictError("Пользователь с таким именем уже существует") from exc
    logger.info("Registered user %s", name)
    return get_user(name) or {}


def login_user(username: str, password: str) -> dict:
    """Authenticate a user and return their public representation."""
    name = str(username or "").strip()
    try:
        with user_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.error("login lookup failed: %s", exc)
        raise AuthError("Внутренняя ошибка входа") from exc

    if row is None or not verify_password(password, row["pwd_hash"]):
        # Same message for both cases: do not leak which usernames exist.
        raise AuthError("Неверное имя пользователя или пароль")

    if needs_rehash(row["pwd_hash"]):
        try:
            with user_connection() as conn:
                conn.execute("UPDATE users SET pwd_hash=? WHERE username=?", (hash_password(password), name))
            logger.info("Upgraded password hash for %s", name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hash upgrade failed for %s: %s", name, exc)

    _touch_last_seen(name)
    return get_user(name) or {}


def _touch_last_seen(username: str) -> None:
    try:
        with user_connection() as conn:
            conn.execute("UPDATE users SET last_seen=? WHERE username=?", (int(time.time()), username))
    except Exception as exc:  # noqa: BLE001
        logger.debug("touch_last_seen(%s): %s", username, exc)


def get_user(username: str) -> dict | None:
    """Return the public fields of a user, or ``None``."""
    if not username:
        return None
    try:
        with user_connection() as conn:
            row = conn.execute(
                "SELECT username, xp, coins, games_played, wins, total_score, created_at, last_seen "
                "FROM users WHERE username=?",
                (username,),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        logger.error("get_user(%s): %s", username, exc)
        return None


def user_exists(username: str) -> bool:
    """Return ``True`` when the account exists."""
    return get_user(username) is not None


def level_from_xp(xp: int) -> int:
    """Convert total XP into a 1-based level number."""
    return max(1, int(math.sqrt(max(0, int(xp)) / XP_PER_LEVEL_FACTOR)) + 1)


def xp_for_next_level(level: int) -> int:
    """Total XP required to reach ``level + 1``."""
    return XP_PER_LEVEL_FACTOR * (max(1, int(level)) ** 2)


def update_user_stats(username: str, score: int, won: bool, xp_gain: int) -> None:
    """Apply a finished game's outcome to a user's aggregate statistics."""
    try:
        with user_connection() as conn:
            conn.execute(
                """
                UPDATE users SET
                    games_played = games_played + 1,
                    total_score  = total_score + ?,
                    wins         = wins + ?,
                    xp           = xp + ?,
                    last_seen    = ?
                WHERE username=?
                """,
                (int(score), 1 if won else 0, max(0, int(xp_gain)), int(time.time()), username),
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("update_user_stats(%s): %s", username, exc)


def add_coins(username: str, amount: int) -> int:
    """Add ``amount`` coins (never below zero).  Returns the new balance."""
    try:
        with user_connection() as conn:
            conn.execute("UPDATE users SET coins=MAX(0, coins+?) WHERE username=?", (int(amount), username))
            row = conn.execute("SELECT coins FROM users WHERE username=?", (username,)).fetchone()
        return int(row["coins"]) if row else 0
    except Exception as exc:  # noqa: BLE001
        logger.error("add_coins(%s): %s", username, exc)
        return 0


def spend_coins(username: str, amount: int) -> bool:
    """Atomically deduct ``amount`` coins; return ``False`` when insufficient."""
    amount = int(amount)
    if amount < 0:
        raise ValidationError("amount must be non-negative")
    try:
        with user_connection() as conn:
            cursor = conn.execute(
                "UPDATE users SET coins = coins - ? WHERE username=? AND coins >= ?",
                (amount, username, amount),
            )
            return cursor.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        logger.error("spend_coins(%s): %s", username, exc)
        return False


def delete_user(username: str) -> bool:
    """Delete an account and every row that references it."""
    try:
        with user_connection() as conn:
            conn.execute("DELETE FROM users WHERE username=?", (username,))
            conn.execute("DELETE FROM achievements WHERE username=?", (username,))
            conn.execute("DELETE FROM campaign_progress WHERE username=?", (username,))
            conn.execute("DELETE FROM ugc_questions WHERE author=?", (username,))
            conn.execute("DELETE FROM ugc_votes WHERE voter=?", (username,))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("delete_user(%s): %s", username, exc)
        return False


def set_user_fields(username: str, *, coins: int | None = None, xp: int | None = None) -> bool:
    """Administrative edit of a user's coins/xp."""
    updates, params = [], []
    if coins is not None:
        updates.append("coins=?")
        params.append(max(0, int(coins)))
    if xp is not None:
        updates.append("xp=?")
        params.append(max(0, int(xp)))
    if not updates:
        return False
    params.append(username)
    try:
        with user_connection() as conn:
            conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE username=?", tuple(params))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("set_user_fields(%s): %s", username, exc)
        return False
