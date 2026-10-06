"""Shop, achievements and profile routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from ..errors import NotFoundError, ValidationError
from ..services import accounts, achievements, campaign, leaderboard
from ..validation import as_str
from .helpers import require_user

bp = Blueprint("profile", __name__, url_prefix="/api")

SHOP_ITEMS = {
    "hint_free":     {"name": "Бесплатная подсказка", "cost": 30,  "desc": "Подсказка без списания очков"},
    "skip_question": {"name": "Пропуск вопроса",      "cost": 50,  "desc": "Пропусти 1 вопрос в кампании"},
    "double_xp":     {"name": "Двойной XP (1 игра)",  "cost": 100, "desc": "Удвоение XP в следующей игре"},
}


# ── Achievements ─────────────────────────────────────────────
@bp.get("/achievements")
def list_achievements():
    """Return all achievements with unlock state."""
    username = session.get("username")
    if not username:
        return jsonify({"achievements": []})
    return jsonify({"achievements": achievements.get_all(username)})


# ── Shop ─────────────────────────────────────────────────────
@bp.get("/shop/items")
def shop_items():
    """Return the catalogue and the caller's coin balance."""
    username = session.get("username")
    coins = (accounts.get_user(username) or {}).get("coins", 0) if username else 0
    return jsonify({"items": SHOP_ITEMS, "coins": coins})


@bp.post("/shop/buy")
def shop_buy():
    """Purchase an item, deducting coins atomically."""
    username = require_user()
    body = request.get_json(silent=True) or {}
    item_id = as_str(body.get("item_id"))
    item = SHOP_ITEMS.get(item_id)
    if item is None:
        raise NotFoundError("Товар не найден")
    if not accounts.spend_coins(username, item["cost"]):
        raise ValidationError("Недостаточно монет")
    user = accounts.get_user(username) or {}
    return jsonify({"ok": True, "item": item_id, "coins_left": user.get("coins", 0)})


# ── Profiles ──────────────────────────────────────────────────
@bp.get("/profile")
def my_profile():
    """Return the caller's full profile."""
    username = require_user()
    user = accounts.get_user(username)
    if not user:
        raise NotFoundError("Пользователь не найден")
    user["level"] = accounts.level_from_xp(user.get("xp", 0))
    user["xp_next_level"] = accounts.xp_for_next_level(user["level"])
    user["achievements"] = achievements.get_all(username)
    user["campaign_progress"] = campaign.get_progress(username)
    return jsonify({"profile": user})


@bp.get("/profile/<path:username>")
def public_profile(username: str):
    """Return a trimmed, public view of another player's profile."""
    user = accounts.get_user(username)
    if not user:
        raise NotFoundError("Пользователь не найден")
    achievements_list = achievements.get_all(username)
    return jsonify({"profile": {
        "username": user["username"],
        "total_score": user["total_score"],
        "games_played": user["games_played"],
        "wins": user["wins"],
        "xp": user["xp"],
        "level": accounts.level_from_xp(user.get("xp", 0)),
        "achievements_count": sum(1 for a in achievements_list if a["unlocked"]),
        "rank": leaderboard.player_rank(username).get("rank", 0),
    }})


@bp.post("/profile/delete")
def delete_profile():
    """Delete the caller's account after explicit confirmation."""
    username = require_user()
    body = request.get_json(silent=True) or {}
    if as_str(body.get("confirm")) != "DELETE_MY_ACCOUNT":
        raise ValidationError("Подтвердите удаление: DELETE_MY_ACCOUNT")
    accounts.delete_user(username)
    session.clear()
    return jsonify({"ok": True, "message": "Аккаунт удалён"})
