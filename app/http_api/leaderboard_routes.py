"""Leaderboard and ranking routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..services import leaderboard
from ..validation import as_int

bp = Blueprint("leaderboard", __name__, url_prefix="/api")


@bp.get("/leaderboard")
def top():
    """Return the global leaderboard."""
    limit = as_int(request.args.get("n", 50), default=50, minimum=1, maximum=200)
    return jsonify(leaderboard.top_players(limit))


@bp.get("/rank/<path:username>")
def rank(username: str):
    """Return a single player's leaderboard position."""
    return jsonify(leaderboard.player_rank(username))
