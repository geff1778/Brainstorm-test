"""Achievements service.

A small declarative rule table drives both manual unlocks (e.g. "used learn
mode") and evaluated unlocks (e.g. "played 10 games").
"""

from __future__ import annotations

import time

from ..db import user_connection
from ..logging_setup import get_logger
from . import accounts, ugc

logger = get_logger(__name__)

ACHIEVEMENTS: dict[str, dict] = {
    "first_win":       {"title": "Первая победа",     "icon": "🏆", "desc": "Выиграй первую игру"},
    "streak5":         {"title": "В потоке",          "icon": "🔥", "desc": "Серия 5 правильных подряд"},
    "campaign_world1": {"title": "Покоритель мира 1", "icon": "🌍", "desc": "Пройди все уровни мира 1"},
    "campaign_boss":   {"title": "Боссубийца",        "icon": "⚔️", "desc": "Победи босс-уровень"},
    "ugc_creator":     {"title": "Автор",             "icon": "✏️", "desc": "Создай первый вопрос"},
    "ugc_10":          {"title": "Контрибьютор",      "icon": "📝", "desc": "Создай 10 вопросов"},
    "games10":         {"title": "Завсегдатай",       "icon": "🎮", "desc": "Сыграй 10 игр"},
    "games50":         {"title": "Ветеран",           "icon": "🎖️", "desc": "Сыграй 50 игр"},
    "perfect_level":   {"title": "Перфекционист",     "icon": "⭐", "desc": "Пройди уровень на 3 звезды"},
    "learn_mode":      {"title": "Студент",           "icon": "📚", "desc": "Используй режим обучения"},
}


def unlock(username: str, achievement_id: str) -> bool:
    """Unlock an achievement.  Returns ``True`` only the first time."""
    if achievement_id not in ACHIEVEMENTS:
        logger.warning("Unknown achievement id: %s", achievement_id)
        return False
    try:
        with user_connection() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO achievements(username, ach_id, unlocked_at) VALUES(?,?,?)",
                (username, achievement_id, int(time.time())),
            )
            inserted = cursor.rowcount > 0
        if inserted:
            logger.info("Achievement %s unlocked for %s", achievement_id, username)
        return inserted
    except Exception as exc:  # noqa: BLE001
        logger.error("unlock(%s, %s): %s", username, achievement_id, exc)
        return False


def get_all(username: str) -> list[dict]:
    """Return every achievement with its unlock state for ``username``."""
    try:
        with user_connection() as conn:
            rows = conn.execute(
                "SELECT ach_id, unlocked_at FROM achievements WHERE username=?", (username,)
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.error("get_all(%s): %s", username, exc)
        return []
    unlocked = {r["ach_id"]: r["unlocked_at"] for r in rows}
    return [
        {"id": ach_id, **meta, "unlocked": ach_id in unlocked, "unlocked_at": unlocked.get(ach_id)}
        for ach_id, meta in ACHIEVEMENTS.items()
    ]


def evaluate(username: str) -> list[str]:
    """Evaluate data-driven rules and return newly unlocked achievement ids."""
    user = accounts.get_user(username)
    if not user:
        return []
    newly: list[str] = []
    if user["wins"] >= 1 and unlock(username, "first_win"):
        newly.append("first_win")
    if user["games_played"] >= 10 and unlock(username, "games10"):
        newly.append("games10")
    if user["games_played"] >= 50 and unlock(username, "games50"):
        newly.append("games50")
    count = ugc.author_question_count(username)
    if count >= 1 and unlock(username, "ugc_creator"):
        newly.append("ugc_creator")
    if count >= 10 and unlock(username, "ugc_10"):
        newly.append("ugc_10")
    return newly
