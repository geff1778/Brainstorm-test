"""Campaign (single-player story mode) service.

Twelve levels grouped into three worlds.  Star ratings gate progression and
reward coins/XP.  Rewards are only granted for *new* stars, so replaying a level
cannot be farmed for currency.
"""

from __future__ import annotations

import time

from ..db import user_connection
from ..errors import NotFoundError, ValidationError
from ..logging_setup import get_logger
from . import accounts

logger = get_logger(__name__)

WORLD_NAMES = {1: "🌍 Мир знаний", 2: "🎭 Мир культуры", 3: "🔮 Мир мастеров"}

CAMPAIGN_LEVELS: list[dict] = [
    {"id": 1,  "title": "Что? Где? Когда?",  "topic": "общие знания",         "world": 1, "difficulty": "easy",   "questions": 8,  "req_stars": 0,  "boss": False, "reward_coins": 30},
    {"id": 2,  "title": "История мира",      "topic": "история",              "world": 1, "difficulty": "easy",   "questions": 8,  "req_stars": 2,  "boss": False, "reward_coins": 30},
    {"id": 3,  "title": "Наука и природа",   "topic": "наука и природа",      "world": 1, "difficulty": "easy",   "questions": 8,  "req_stars": 5,  "boss": False, "reward_coins": 30},
    {"id": 4,  "title": "Финал Мира 1 🏆",   "topic": "общие знания",         "world": 1, "difficulty": "medium", "questions": 10, "req_stars": 9,  "boss": True,  "reward_coins": 80},
    {"id": 5,  "title": "Кино и музыка",     "topic": "кино и музыка",        "world": 2, "difficulty": "medium", "questions": 8,  "req_stars": 12, "boss": False, "reward_coins": 40},
    {"id": 6,  "title": "Спорт",             "topic": "спорт",                "world": 2, "difficulty": "medium", "questions": 8,  "req_stars": 14, "boss": False, "reward_coins": 40},
    {"id": 7,  "title": "География",         "topic": "география",            "world": 2, "difficulty": "medium", "questions": 8,  "req_stars": 16, "boss": False, "reward_coins": 40},
    {"id": 8,  "title": "Технологии",        "topic": "технологии и IT",      "world": 2, "difficulty": "medium", "questions": 8,  "req_stars": 18, "boss": False, "reward_coins": 40},
    {"id": 9,  "title": "Финал Мира 2 🏆",   "topic": "смешанная",            "world": 2, "difficulty": "hard",   "questions": 12, "req_stars": 22, "boss": True,  "reward_coins": 120},
    {"id": 10, "title": "Мифология",         "topic": "мифология",            "world": 3, "difficulty": "hard",   "questions": 8,  "req_stars": 25, "boss": False, "reward_coins": 50},
    {"id": 11, "title": "Литература",        "topic": "литература",           "world": 3, "difficulty": "hard",   "questions": 8,  "req_stars": 27, "boss": False, "reward_coins": 50},
    {"id": 12, "title": "Эрудит Финал 🌟",   "topic": "эрудит высший уровень", "world": 3, "difficulty": "hard",  "questions": 15, "req_stars": 30, "boss": True,  "reward_coins": 200},
]

XP_PER_CORRECT = 15
XP_PER_STAR = 25


def get_level(level_id: int) -> dict:
    """Return level metadata or raise :class:`NotFoundError`."""
    level = next((item for item in CAMPAIGN_LEVELS if item["id"] == int(level_id)), None)
    if level is None:
        raise NotFoundError("Уровень не найден")
    return level


def get_progress(username: str) -> dict:
    """Return the full campaign board for ``username`` with lock flags."""
    try:
        with user_connection() as conn:
            rows = conn.execute(
                "SELECT level_id, stars, best_score FROM campaign_progress WHERE username=?",
                (username,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.error("get_progress(%s): %s", username, exc)
        return {"levels": [], "total_stars": 0, "worlds": WORLD_NAMES}

    progress = {int(r["level_id"]): {"stars": int(r["stars"]), "best_score": int(r["best_score"])} for r in rows}
    total_stars = sum(v["stars"] for v in progress.values())
    levels = []
    for level in CAMPAIGN_LEVELS:
        saved = progress.get(level["id"], {"stars": 0, "best_score": 0})
        levels.append({
            **level,
            "world_name": WORLD_NAMES.get(level["world"], ""),
            "stars": saved["stars"],
            "best_score": saved["best_score"],
            "locked": total_stars < level["req_stars"],
        })
    return {"levels": levels, "total_stars": total_stars, "worlds": WORLD_NAMES}


def stars_for(correct: int, total_questions: int) -> int:
    """Convert a correct/total ratio into a 0-3 star rating."""
    if total_questions <= 0:
        return 0
    pct = correct / total_questions
    if pct >= 0.9:
        return 3
    if pct >= 0.65:
        return 2
    if pct >= 0.4:
        return 1
    return 0


def save_result(username: str, level_id: int, score: int, correct: int, total_questions: int) -> dict:
    """Persist a level result and award coins/XP for new stars."""
    if total_questions <= 0:
        raise ValidationError("total_questions must be positive")
    level = get_level(level_id)
    score = max(0, int(score))
    correct = max(0, min(int(correct), int(total_questions)))
    stars = stars_for(correct, total_questions)
    xp_earned = correct * XP_PER_CORRECT + stars * XP_PER_STAR
    coins_earned = 0

    try:
        with user_connection() as conn:
            existing = conn.execute(
                "SELECT stars, best_score FROM campaign_progress WHERE username=? AND level_id=?",
                (username, level["id"]),
            ).fetchone()
            old_stars = int(existing["stars"]) if existing else 0
            new_stars = max(old_stars, stars)
            if stars > old_stars:
                coins_earned = level["reward_coins"] * (stars - old_stars) // 3
            conn.execute(
                """
                INSERT INTO campaign_progress(username, level_id, stars, best_score, completed_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(username, level_id) DO UPDATE SET
                    stars       = MAX(excluded.stars, stars),
                    best_score  = MAX(excluded.best_score, best_score),
                    completed_at = excluded.completed_at
                """,
                (username, level["id"], new_stars, score, int(time.time())),
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("save_result(%s, %s): %s", username, level_id, exc)
        return {"stars": stars, "xp": 0, "coins": 0, "pct": round(correct / total_questions * 100)}

    if coins_earned > 0:
        accounts.add_coins(username, coins_earned)
    if xp_earned > 0:
        try:
            with user_connection() as conn:
                conn.execute("UPDATE users SET xp=xp+? WHERE username=?", (xp_earned, username))
        except Exception as exc:  # noqa: BLE001
            logger.error("save_result xp(%s): %s", username, exc)

    return {
        "stars": stars,
        "xp": xp_earned,
        "coins": coins_earned,
        "pct": round(correct / total_questions * 100),
        "new_best": score,
    }
