"""Campaign (story mode) routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from ..errors import AuthError
from ..services import achievements, ai_client, campaign
from ..validation import as_int
from .helpers import require_user

bp = Blueprint("campaign", __name__, url_prefix="/api/campaign")


@bp.get("/progress")
def progress():
    """Return the current user's campaign progress."""
    username = require_user()
    return jsonify(campaign.get_progress(username))


@bp.get("/levels")
def levels():
    """Return all levels; guests see them locked."""
    username = session.get("username")
    if username:
        return jsonify(campaign.get_progress(username))
    return jsonify({
        "levels": [{**level, "stars": 0, "best_score": 0, "locked": True}
                   for level in campaign.CAMPAIGN_LEVELS],
        "total_stars": 0,
        "worlds": campaign.WORLD_NAMES,
    })


@bp.post("/start")
def start():
    """Generate the questions for a level the player has unlocked."""
    username = require_user()
    body = request.get_json(silent=True) or {}
    level = campaign.get_level(as_int(body.get("level_id", 0), default=0))
    total_stars = campaign.get_progress(username)["total_stars"]
    if total_stars < level["req_stars"]:
        raise AuthError(f"Нужно {level['req_stars']} звёзд")
    questions = ai_client.generate_questions(
        topic=level["topic"], count=level["questions"],
        difficulty=level["difficulty"], num_options=4,
    )
    return jsonify({"questions": questions, "level": level})


@bp.post("/result")
def result():
    """Record a finished level and return rewards."""
    username = require_user()
    body = request.get_json(silent=True) or {}
    outcome = campaign.save_result(
        username,
        as_int(body.get("level_id", 0), default=0),
        as_int(body.get("score", 0), default=0, minimum=0),
        as_int(body.get("correct", 0), default=0, minimum=0),
        as_int(body.get("total_questions", 1), default=1, minimum=1),
    )
    if outcome["stars"] == 3:
        achievements.unlock(username, "perfect_level")
    outcome["new_achievements"] = achievements.evaluate(username)
    return jsonify(outcome)
