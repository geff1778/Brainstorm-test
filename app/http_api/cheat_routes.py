"""Cheat-mode activation and diagnostics routes.

The cheat code is a *shared secret* read from the environment.  Activation is
rate limited so the code cannot be brute forced.  The code itself is never
echoed into logs.

Besides activation this module exposes read-only diagnostics used by the cheat
panel (currently the live answer distribution for a room).
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from ..config import settings
from ..domain.registry import registry
from ..logging_setup import get_logger
from ..security import RateLimiter, client_ip, safe_equal
from ..validation import as_str

bp = Blueprint("cheat", __name__, url_prefix="/api/cheat")
logger = get_logger(__name__)

activation_limiter = RateLimiter(5, 300)


def _client_ip() -> str:
    return client_ip(request.remote_addr, request.headers.get("X-Forwarded-For"),
                     trust_proxy=settings.trust_proxy)


def _is_privileged() -> bool:
    """Cheat or admin sessions may read cheat diagnostics."""
    return bool(session.get("is_tester") or session.get("is_admin"))


@bp.post("/activate")
def activate():
    """Activate tester (cheat) mode for this session."""
    ip = _client_ip()
    if not activation_limiter.allow(ip):
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


@bp.get("/room_stats/<code>")
def room_stats(code: str):
    """Live answer distribution for a room (cheat/admin only)."""
    if not _is_privileged():
        return jsonify({"ok": False, "error": "Нет доступа"}), 403
    room = registry.get(as_str(code, max_len=8).upper())
    if room is None:
        return jsonify({"ok": False, "error": "Комната не найдена"}), 404
    stats = room.answer_distribution()
    stats["ok"] = True
    stats["state"] = room.state
    stats["question_number"] = room.current_q + 1
    stats["total_questions"] = room.total_questions
    return jsonify(stats)


@bp.post("/logout")
def logout():
    """Deactivate cheat and admin mode."""
    session.pop("is_tester", None)
    session.pop("tester_username", None)
    session.pop("is_admin", None)
    return jsonify({"ok": True})
