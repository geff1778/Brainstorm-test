"""Room discovery routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..domain.registry import registry
from ..validation import as_str

bp = Blueprint("rooms", __name__, url_prefix="/api")

#: Room states that are worth advertising to other players.
_VISIBLE_STATES = ("waiting", "playing")


@bp.get("/public_rooms")
def public_rooms():
    """List joinable public rooms."""
    rooms = [
        {
            "code": room.code,
            "state": room.state,
            "players": len(room.human_players),
            "mode": room.mode,
            "topic": room.settings.get("topic", ""),
        }
        for room in registry.values()
        if room.is_public and room.state in _VISIBLE_STATES
    ]
    return jsonify(rooms)


@bp.get("/rooms")
def all_rooms():
    """List every live room (public metadata only)."""
    return jsonify([
        {"code": room.code, "state": room.state, "players": len(room.players), "mode": room.mode}
        for room in registry.values()
    ])


@bp.post("/check_session")
def check_session():
    """Tell the client whether a nickname is already present in a room."""
    body = request.get_json(silent=True) or {}
    code = as_str(body.get("room_code")).upper()
    name = as_str(body.get("player_name"))
    if not code or not name:
        return jsonify({"ok": True, "already_in": False})
    room = registry.get(code)
    if room is None:
        return jsonify({"ok": True, "already_in": False})
    already = room.name_taken(name)
    return jsonify({"ok": True, "already_in": already})
