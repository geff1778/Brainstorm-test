"""Authentication routes: register, login, logout, current user."""

from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from ..services import accounts, achievements
from ..validation import as_str
from .helpers import auth_limiter, rate_limit

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@bp.post("/register")
@rate_limit(auth_limiter, key_prefix="register")
def register():
    """Create an account and start a session."""
    body = request.get_json(silent=True) or {}
    user = accounts.register_user(as_str(body.get("username")), as_str(body.get("password"), strip=False))
    session.clear()
    session["username"] = user["username"]
    user["level"] = accounts.level_from_xp(user.get("xp", 0))
    return jsonify({"ok": True, "user": user})


@bp.post("/login")
@rate_limit(auth_limiter, key_prefix="login")
def login():
    """Authenticate an existing account."""
    body = request.get_json(silent=True) or {}
    user = accounts.login_user(as_str(body.get("username")), as_str(body.get("password"), strip=False))
    session.clear()
    session["username"] = user["username"]
    user["level"] = accounts.level_from_xp(user.get("xp", 0))
    return jsonify({"ok": True, "user": user})


@bp.post("/logout")
def logout():
    """Clear the session."""
    session.clear()
    return jsonify({"ok": True})


@bp.get("/me")
def me():
    """Return the current session's user, if any."""
    username = session.get("username")
    if not username:
        return jsonify({"logged_in": False})
    user = accounts.get_user(username)
    if not user:
        session.pop("username", None)
        return jsonify({"logged_in": False})
    user["level"] = accounts.level_from_xp(user.get("xp", 0))
    user["achievements"] = achievements.get_all(username)
    user["xp_next_level"] = accounts.xp_for_next_level(user["level"])
    return jsonify({"logged_in": True, "user": user})
