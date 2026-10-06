"""Cheat-mode activation routes.

The cheat code is a *shared secret* read from the environment.  Activation is
rate limited so the six-digit code cannot be brute forced.  The code itself is
never echoed into logs.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from ..config import settings
from ..logging_setup import get_logger
from ..security import RateLimiter, client_ip, safe_equal
from ..validation import as_str

bp = Blueprint("cheat", __name__, url_prefix="/api/cheat")
logger = get_logger(__name__)

_activation_limiter = RateLimiter(5, 300)


def _client_ip() -> str:
    return client_ip(request.remote_addr, request.headers.get("X-Forwarded-For"),
                     trust_proxy=settings.trust_proxy)


@bp.post("/activate")
def activate():
    """Activate tester (cheat) mode for this session."""
    ip = _client_ip()
    if not _activation_limiter.allow(ip):
        return jsonify({"ok": False, "error": "Слишком много попыток"}), 429

    body = request.get_json(silent=True) or {}
    code = as_str(body.get("code"))
    username = as_str(body.get("username"), max_len=20)
    if not username:
        return jsonify({"ok": False, "error": "Username required"}), 400

    if safe_equal(code, settings.cheat_tester_code):
        session["is_tester"] = True
        session["tester_username"] = username
        logger.info("Cheat mode activated for %s from %s", username, ip)
        return jsonify({"ok": True})

    logger.warning("Failed cheat activation from %s", ip)
    return jsonify({"ok": False, "error": "Invalid code"}), 403


@bp.get("/check")
def check():
    """Report whether cheat/admin mode is active for this session."""
    return jsonify({
        "is_tester": bool(session.get("is_tester")),
        "is_admin": bool(session.get("is_admin")),
    })


@bp.post("/logout")
def logout():
    """Deactivate cheat and admin mode."""
    session.pop("is_tester", None)
    session.pop("tester_username", None)
    session.pop("is_admin", None)
    return jsonify({"ok": True})
