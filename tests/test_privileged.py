"""Tests for privileged socket events (cheat + admin) and their guards.

The cheat/admin sessions are established over HTTP and then inherited by the
socket via ``flask_test_client``, which mirrors how a real browser shares the
session cookie between the REST calls and the websocket handshake.
"""

from __future__ import annotations

import time


def _collect(client, name, timeout=3.0):
    """Poll the client until an event named ``name`` arrives (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        packets = [p for p in client.get_received() if p["name"] == name]
        if packets:
            return packets[-1]["args"][0]
        time.sleep(0.05)
    return None


def _create_room(sock, name="Хост"):
    sock.emit("create_room", {"player_name": name, "is_public": True})
    return _collect(sock, "room_created")


class TestCheatEvents:
    def test_tester_creates_cheat_room(self, socket_factory, tester_client):
        sock = socket_factory(flask_test_client=tester_client)
        created = _create_room(sock)
        assert created is not None and created["is_host"] is True

    def test_cheat_update_score_emits_ack(self, socket_factory, tester_client):
        sock = socket_factory(flask_test_client=tester_client)
        _create_room(sock)
        sock.emit("cheat_update_score", {"score": 4242})
        ack = _collect(sock, "cheat_score_updated")
        assert ack is not None and ack["score"] == 4242

    def test_cheat_infinite_lives_toggle(self, socket_factory, tester_client):
        sock = socket_factory(flask_test_client=tester_client)
        _create_room(sock)
        sock.emit("cheat_set_infinite_lives", {"enabled": True})
        ack = _collect(sock, "cheat_ack")
        assert ack["feature"] == "infinite_lives" and ack["enabled"] is True

    def test_cheat_events_are_blocked_for_normal_players(self, socket_client):
        created = _create_room(socket_client)
        assert created is not None
        socket_client.emit("cheat_update_score", {"score": 999})
        assert _collect(socket_client, "cheat_score_updated", timeout=1.0) is None


class TestAdminEvents:
    def test_admin_kick_removes_player(self, socket_factory, admin_client):
        host = socket_factory()
        created = _create_room(host)
        code = created["room_code"]

        guest = socket_factory()
        guest.emit("join_room", {"room_code": code, "player_name": "Гость"})
        _collect(guest, "room_joined")

        admin = socket_factory(flask_test_client=admin_client)
        admin.emit("admin_kick_player", {"room_code": code, "player_name": "Гость"})
        result = _collect(admin, "admin_action_result")
        assert result is not None and result["ok"] is True

        from app.domain.registry import registry
        room = registry.get(code)
        assert all(p.name != "Гость" for p in room.players.values())

    def test_admin_kick_without_room_code_fails(self, socket_factory, admin_client):
        admin = socket_factory(flask_test_client=admin_client)
        admin.emit("admin_kick_player", {"player_name": "Гость"})
        result = _collect(admin, "admin_action_result")
        assert result is not None and result["ok"] is False

    def test_admin_event_without_privileges_is_rejected(self, socket_client):
        socket_client.emit("admin_force_end_game", {"room_code": "ZZZZZZ"})
        result = _collect(socket_client, "admin_action_result", timeout=1.0)
        assert result is not None and result["ok"] is False


class TestSvoyaigra:
    def test_select_cell_rejects_non_svoyaigra_room(self, socket_client):
        _create_room(socket_client)
        socket_client.emit("svoyaigra_select_cell", {"row": 0, "col": 0})
        # No svoyaigra state exists, so nothing should be broadcast.
        assert _collect(socket_client, "svoyaigra_question", timeout=0.5) is None
