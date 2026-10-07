"""UGC (user generated content) routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..services import accounts, achievements, ugc
from ..validation import as_int, as_str
from .helpers import api_limiter, rate_limit, require_user

bp = Blueprint("ugc", __name__, url_prefix="/api/ugc")

COINS_PER_QUESTION = 5


@bp.get("/questions")
def list_questions():
    """List approved UGC questions."""
    topic = as_str(request.args.get("topic", ""), max_len=50)
    limit = as_int(request.args.get("limit", 50), default=50, minimum=1, maximum=100)
    offset = as_int(request.args.get("offset", 0), default=0, minimum=0)
    questions = ugc.list_questions(topic=topic, status=ugc.STATUS_APPROVED, limit=limit, offset=offset)
    return jsonify({"questions": questions, "count": len(questions)})


@bp.get("/my")
def my_questions():
    """List the current user's submissions."""
    username = require_user()
    return jsonify({"questions": ugc.list_by_author(username)})


@bp.post("/create")
@rate_limit(api_limiter, key_prefix="ugc_create", by_user=True)
def create():
    """Submit a new UGC question."""
    username = require_user()
    body = request.get_json(silent=True) or {}
    options = body.get("options")
    question_id = ugc.create_question(
        author=username,
        question=as_str(body.get("question"), max_len=300),
        options=options if isinstance(options, list) else [],
        correct=as_int(body.get("correct", 0), default=0),
        topic=as_str(body.get("topic", ""), max_len=50),
        difficulty=as_int(body.get("difficulty", 2), default=2, minimum=1, maximum=5),
    )
    coins = COINS_PER_QUESTION
    accounts.add_coins(username, coins)
    new_achievements = achievements.evaluate(username)
    return jsonify({"ok": True, "id": question_id, "coins_earned": coins,
                    "new_achievements": new_achievements})


@bp.post("/vote")
def vote():
    """Vote a question up or down."""
    username = require_user()
    body = request.get_json(silent=True) or {}
    ok = ugc.vote(as_int(body.get("question_id", 0), default=0),
                  username, as_int(body.get("vote", 1), default=1))
    return jsonify({"ok": ok})


@bp.post("/report")
@rate_limit(api_limiter, key_prefix="ugc_report", by_user=True)
def report():
    """Report an inappropriate question."""
    username = require_user()
    body = request.get_json(silent=True) or {}
    ok = ugc.report(as_int(body.get("question_id", 0), default=0), username,
                    as_str(body.get("reason", ""), max_len=200))
    return jsonify({"ok": ok})
