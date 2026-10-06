"""End-to-end socket flow tests.

These exercise the real game loop through SocketIO's test client: create a room,
join with a second player, start a game (using the offline fallback bank) and
answer a question.  They are the safety net that would have caught the missing
``start_game``/chat handlers.
"""

from __future__ import annotations

import time

import pytest


def _events(client, name):
    return [packet for packet in client.get_received() if packet["name"] == name]


class EventBuffer:
    """Collects socket packets across polls so nothing is dropped.

    ``socketio.test_client`` drains its receive queue on every ``get_received``
    call, so a naive poller can discard events that arrived in the same batch.
    """

    def __init__(self, client):
        self.client = client
        self._packets = []

    def drain(self):
        self._packets.extend(self.client.get_received())

    def find(self, name):
        self.drain()
        matches = [p for p in self._packets if p["name"] == name]
        return matches[-1]["args"][0] if matches else None

    def wait(self, name, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.find(name)
            if found is not None:
                return found
            time.sleep(0.05)
        return None


def _last(client, name):
    packets = _events(client, name)
    return packets[-1]["args"][0] if packets else None


def _wait_for(client, name, timeout=5.0):
    """Poll the test client until an event arrives."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        packet = _last(client, name)
        if packet is not None:
            return packet
        time.sleep(0.05)
    return None


@pytest.fixture()
def two_clients(app):
    from app import socketio
    host = socketio.test_client(app)
    guest = socketio.test_client(app)
    yield host, guest
    for client in (host, guest):
        if client.is_connected():
            client.disconnect()


class TestRoomLifecycle:
    def test_create_room_returns_code(self, two_clients):
        host, _ = two_clients
        host.emit("create_room", {"player_name": "Host"})
        payload = _wait_for(host, "room_created")
        assert payload and len(payload["room_code"]) == 6
        assert payload["is_host"] is True

    def test_guest_can_join(self, two_clients):
        host, guest = two_clients
        host.emit("create_room", {"player_name": "Host"})
        code = _wait_for(host, "room_created")["room_code"]
        guest.emit("join_room", {"room_code": code, "player_name": "Guest"})
        payload = _wait_for(guest, "room_joined")
        assert payload is not None
        assert len(payload["players"]) == 2

    def test_duplicate_nick_is_rejected(self, two_clients):
        host, guest = two_clients
        host.emit("create_room", {"player_name": "Same"})
        code = _wait_for(host, "room_created")["room_code"]
        guest.emit("join_room", {"room_code": code, "player_name": "same"})
        error = _wait_for(guest, "error")
        assert error and "занят" in error["message"]

    def test_join_missing_room_errors(self, two_clients):
        _, guest = two_clients
        guest.emit("join_room", {"room_code": "ZZZZZZ", "player_name": "Nobody"})
        assert _wait_for(guest, "error") is not None


class TestGameLoop:
    def test_start_game_emits_questions_and_answers(self, two_clients):
        host, guest = two_clients
        host_buffer = EventBuffer(host)
        host.emit("create_room", {"player_name": "Host"})
        code = host_buffer.wait("room_created")["room_code"]
        guest.emit("join_room", {"room_code": code, "player_name": "Guest"})
        EventBuffer(guest).wait("room_joined")

        host.emit("update_settings", {"question_count": 2, "difficulty": "easy", "topic": "тест"})
        host.emit("start_game", {})
        assert host_buffer.wait("game_started") is not None, "start_game must emit game_started"

        question = host_buffer.wait("new_question")
        assert question is not None and question["question"]["options"]

        host.emit("submit_answer", {"answer_index": 0})
        ack = host_buffer.wait("answer_ack")
        assert ack is not None and "correct" in ack

    def test_chat_round_trip(self, two_clients):
        host, guest = two_clients
        host_buffer = EventBuffer(host)
        host.emit("create_room", {"player_name": "Host"})
        code = host_buffer.wait("room_created")["room_code"]
        guest.emit("join_room", {"room_code": code, "player_name": "Guest"})
        EventBuffer(guest).wait("room_joined")
        host_buffer.drain()

        guest.emit("chat_message", {"message": "привет"})
        message = host_buffer.wait("chat_message")
        assert message is not None and message["text"] == "привет"

    def test_heartbeat_is_acknowledged(self, two_clients):
        host, _ = two_clients
        buffer = EventBuffer(host)
        host.emit("create_room", {"player_name": "Host"})
        buffer.wait("room_created")
        host.emit("heartbeat", {})
        assert buffer.wait("heartbeat_ack") is not None

    def test_restart_returns_to_lobby(self, two_clients):
        host, _ = two_clients
        buffer = EventBuffer(host)
        host.emit("create_room", {"player_name": "Host"})
        buffer.wait("room_created")
        host.emit("restart_room", {"keep_scores": False})
        payload = buffer.wait("room_restarted")
        assert payload is not None and payload["keep_scores"] is False


class TestCheatGating:
    def test_cheat_events_ignored_without_activation(self, two_clients):
        host, _ = two_clients
        buffer = EventBuffer(host)
        host.emit("create_room", {"player_name": "Host"})
        buffer.wait("room_created")
        host.emit("cheat_add_score_all", {"amount": 999})
        time.sleep(0.3)
        # No cheat_ack should be produced for a non-cheater.
        assert buffer.find("cheat_ack") is None
