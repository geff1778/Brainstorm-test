"""Administration routes.

Every endpoint here requires ``session['is_admin']``.  The admin key is read from
the environment, compared in constant time and never logged.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from ..config import settings
from ..domain.registry import registry
from ..logging_setup import get_logger
from ..security import RateLimiter, client_ip, safe_equal
from ..services import accounts, leaderboard, ugc
from ..validation import as_int, as_str, validate_username
from .helpers import require_admin

bp = Blueprint("admin", __name__, url_prefix="/api/admin")
logger = get_logger(__name__)

activation_limiter = RateLimiter(5, 300)


def _client_ip() -> str:
    return client_ip(request.remote_addr, request.headers.get("X-Forwarded-For"),
                     trust_proxy=settings.trust_proxy)


# ── Activation ───────────────────────────────────────────────
@bp.post("/activate")
def activate():
    """Activate admin (or cheat) mode using a shared key."""
    ip = _client_ip()
    if not activation_limiter.allow(ip):
        return jsonify({"ok": False, "error": "Слишком много попыток"}), 429
    body = request.get_json(silent=True) or {}
    key = as_str(body.get("key"))
    if safe_equal(key, settings.admin_secret_key):
        session["is_admin"] = True
        logger.info("Admin activated from %s", ip)
        return jsonify({"ok": True, "role": "admin"})
    if safe_equal(key, settings.cheat_tester_code):
        session["is_tester"] = True
        session["tester_username"] = session.get("username", "tester")
        return jsonify({"ok": True, "role": "cheat"})
    logger.warning("Failed admin activation from %s", ip)
    return jsonify({"ok": False, "error": "Invalid key"}), 403


@bp.post("/logout")
def logout():
    """Deactivate admin mode."""
    session.pop("is_admin", None)
    return jsonify({"ok": True})


# ── Rooms ────────────────────────────────────────────────────
@bp.get("/rooms")
def list_rooms():
    """List all live rooms with operational detail."""
    require_admin()
    now = int(__import__("time").time())
    return jsonify([{
        "code": room.code,
        "state": room.state,
        "players": len(room.players),
        "mode": room.mode,
        "host": room.players[room.host_sid].name if room.host_sid in room.players else "",
        "is_public": room.is_public,
        "is_sandbox": room.is_sandbox,
        "topic": room.settings.get("topic", ""),
        "idle_secs": now - int(room.last_activity),
    } for room in registry.values()])


@bp.post("/rooms/cleanup")
def cleanup_rooms():
    """Force a sweep of empty and idle rooms."""
    require_admin()
    return jsonify({"ok": True, "removed": registry.cleanup_stale()})


# ── Users ────────────────────────────────────────────────────
@bp.get("/users")
def list_users():
    """Search registered leaderboard entries."""
    require_admin()
    return jsonify(leaderboard.all_players(
        nick=as_str(request.args.get("nick", ""), max_len=50),
        limit=as_int(request.args.get("limit", 200), default=200, minimum=1, maximum=500),
    ))


@bp.post("/reset_user")
def reset_user():
    """Zero a player's aggregate statistics."""
    require_admin()
    body = request.get_json(silent=True) or {}
    return jsonify({"ok": leaderboard.reset_player_stats(as_str(body.get("username")))})


@bp.post("/reset_server")
def reset_server():
    """Purge old history/expired bans and finished rooms."""
    require_admin()
    stats = leaderboard.reset_server_stats()
    removed_rooms = 0
    for code in registry.codes():
        room = registry.get(code)
        if room and room.state == "finished":
            registry.remove(code)
            removed_rooms += 1
    return jsonify({"ok": True, "removed_rooms": removed_rooms, **stats})


# ── Bans ─────────────────────────────────────────────────────
@bp.get("/bans")
def list_bans():
    """List active name bans."""
    require_admin()
    return jsonify(leaderboard.list_bans(kind=leaderboard.BAN_KIND_NAME))


@bp.post("/ban")
def ban():
    """Create a name ban."""
    require_admin()
    body = request.get_json(silent=True) or {}
    ok = leaderboard.ban(
        as_str(body.get("identifier")),
        as_str(body.get("reason"), max_len=200),
        as_int(body.get("duration_minutes", 60), default=60, minimum=1),
    )
    return jsonify({"ok": ok})


@bp.post("/unban")
def unban():
    """Remove a name ban."""
    require_admin()
    body = request.get_json(silent=True) or {}
    return jsonify({"ok": leaderboard.unban(as_str(body.get("identifier")))})


# ── IP bans ──────────────────────────────────────────────────
@bp.get("/ip_bans")
def list_ip_bans():
    """List active IP bans."""
    require_admin()
    return jsonify(leaderboard.list_bans(kind=leaderboard.BAN_KIND_IP))


@bp.post("/ip_ban")
def ip_ban():
    """Ban an IP address."""
    require_admin()
    body = request.get_json(silent=True) or {}
    ip = as_str(body.get("ip"))
    if not ip:
        return jsonify({"ok": False, "error": "IP required"}), 400
    ok = leaderboard.ban(ip, as_str(body.get("reason"), max_len=200),
                         as_int(body.get("duration_minutes", 1440), default=1440, minimum=1),
                         kind=leaderboard.BAN_KIND_IP)
    return jsonify({"ok": ok})


@bp.post("/ip_unban")
def ip_unban():
    """Remove an IP ban."""
    require_admin()
    body = request.get_json(silent=True) or {}
    return jsonify({"ok": leaderboard.unban(as_str(body.get("ip")))})


# ── History ──────────────────────────────────────────────────
@bp.get("/room_history/<code>")
def room_history(code: str):
    """Return the recent game history for a room code."""
    require_admin()
    return jsonify(leaderboard.get_room_history(code))


# ── UGC moderation ───────────────────────────────────────────
@bp.get("/ugc_pending")
def ugc_pending():
    """List UGC questions awaiting review."""
    require_admin()
    return jsonify({"questions": ugc.list_pending(50)})


@bp.post("/ugc_moderate")
def ugc_moderate():
    """Approve or reject a UGC question."""
    require_admin()
    body = request.get_json(silent=True) or {}
    ok = ugc.moderate(as_int(body.get("question_id", 0), default=0),
                      bool(body.get("approve", False)),
                      as_str(body.get("reason", ""), max_len=200))
    return jsonify({"ok": ok})


# ── Account administration ───────────────────────────────────
@bp.post("/impersonate")
def impersonate():
    """Temporarily view the site as another user (admin session is preserved)."""
    require_admin()
    body = request.get_json(silent=True) or {}
    username = validate_username(body.get("username"))
    user = accounts.get_user(username)
    if not user:
        return jsonify({"error": "User not found"}), 404
    session["impersonating"] = True
    session["impersonating_user"] = username
    session["username"] = username
    logger.info("Admin impersonating %s", username)
    return jsonify({"ok": True, "user": user, "message": f"Вы вошли как {username}"})


@bp.post("/impersonate/stop")
def stop_impersonate():
    """Stop impersonating and restore the admin-only session."""
    require_admin()
    target = session.pop("impersonating_user", None)
    session.pop("impersonating", None)
    session.pop("username", None)
    return jsonify({"ok": True, "message": f"Вышли из аккаунта {target or ''}".strip()})


@bp.post("/user/<username>/edit")
def edit_user(username: str):
    """Edit a user's coins and/or XP."""
    require_admin()
    body = request.get_json(silent=True) or {}
    coins = as_int(body["coins"], minimum=0) if "coins" in body else None
    xp = as_int(body["xp"], minimum=0) if "xp" in body else None
    if coins is None and xp is None:
        return jsonify({"error": "Нечего обновлять"}), 400
    accounts.set_user_fields(username, coins=coins, xp=xp)
    return jsonify({"ok": True, "user": accounts.get_user(username)})


@bp.post("/user/<username>/delete")
def delete_user(username: str):
    """Delete a user account after explicit confirmation."""
    require_admin()
    body = request.get_json(silent=True) or {}
    if as_str(body.get("confirm")) != "DELETE":
        return jsonify({"error": "Подтвердите удаление: отправьте {\"confirm\": \"DELETE\"}"}), 400
    accounts.delete_user(username)
    logger.warning("Admin deleted user %s", username)
    return jsonify({"ok": True, "message": f"Аккаунт {username} удалён"})
