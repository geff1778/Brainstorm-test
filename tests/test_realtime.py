"""Tests for the realtime layer: registry, cache, chat and the event contract."""

from __future__ import annotations

import re
from pathlib import Path

from app.cache import TTLCache
from app.domain.registry import RoomRegistry
from app.realtime.chat import ChatStore

ROOT = Path(__file__).resolve().parent.parent


class TestRegistry:
    def test_create_indexes_host(self):
        registry = RoomRegistry()
        room = registry.create("sid-1")
        assert registry.get(room.code) is room
        assert registry.get_by_sid("sid-1") is room

    def test_remove_clears_sid_index(self):
        registry = RoomRegistry()
        room = registry.create("sid-1")
        room.add_player("sid-2", "Other")
        registry.index("sid-2", room.code)
        registry.remove(room.code)
        assert registry.get_by_sid("sid-2") is None

    def test_cleanup_removes_empty_rooms(self):
        registry = RoomRegistry(empty_timeout=0)
        registry.create("sid-1").last_activity = 0
        assert registry.cleanup_stale() == 1
        assert len(registry) == 0

    def test_cleanup_keeps_active_rooms(self):
        registry = RoomRegistry(idle_timeout=1000, empty_timeout=1000)
        registry.create("sid-1").add_player("sid-1", "P")
        assert registry.cleanup_stale() == 0

    def test_capacity_limit(self):
        registry = RoomRegistry(max_rooms=2)
        registry.create("a")
        registry.create("b")
        assert registry.at_capacity() is True

    def test_rebind_moves_index(self):
        registry = RoomRegistry()
        room = registry.create("old")
        registry.rebind("old", "new", room.code)
        assert registry.get_by_sid("old") is None
        assert registry.get_by_sid("new") is room


class TestTTLCache:
    def test_set_and_get(self):
        cache = TTLCache(max_entries=2)
        cache.set("a", 1)
        assert cache.get("a") == 1

    def test_missing_key_returns_none(self):
        assert TTLCache().get("nope") is None

    def test_eviction_bounds_size(self):
        cache = TTLCache(max_entries=3)
        for index in range(10):
            cache.set(index, index)
        assert len(cache) == 3
        assert cache.stats()["evictions"] == 7

    def test_expiry(self):
        cache = TTLCache(max_entries=2, ttl_seconds=0)
        cache.set("a", 1)
        assert cache.get("a") is None

    def test_stats_shape(self):
        stats = TTLCache().stats()
        assert {"size", "hits", "misses", "evictions", "ttl"} <= set(stats)


class TestChatStore:
    def test_append_and_history(self):
        store = ChatStore()
        store.append("ROOM", name="A", text="hi")
        assert store.history("ROOM")[0]["text"] == "hi"

    def test_history_is_bounded(self):
        store = ChatStore(max_messages=3)
        for index in range(10):
            store.append("ROOM", name="A", text=str(index))
        assert len(store.history("ROOM")) == 3

    def test_message_length_is_capped(self):
        store = ChatStore()
        record = store.append("ROOM", name="A", text="x" * 1000)
        assert len(record["text"]) == 200

    def test_delete_by_timestamp(self):
        store = ChatStore()
        record = store.append("ROOM", name="A", text="bye")
        assert store.delete("ROOM", record["ts"]) is True
        assert store.history("ROOM") == []


class TestEventContract:
    """Every event the browser emits must have a server handler."""

    @staticmethod
    def _client_events() -> set:
        source = (ROOT / "static" / "js" / "game.js").read_text(encoding="utf-8")
        return set(re.findall(r'socket\.emit\(\s*"([^"]+)"', source))

    @staticmethod
    def _server_handlers() -> set:
        source = (ROOT / "app" / "realtime" / "socket_events.py").read_text(encoding="utf-8")
        return set(re.findall(r'@socketio\.on\(\s*"([^"]+)"', source))

    def test_no_orphan_client_events(self):
        orphans = self._client_events() - self._server_handlers()
        assert not orphans, f"client emits events with no server handler: {sorted(orphans)}"

    def test_critical_handlers_present(self):
        handlers = self._server_handlers()
        required = {
            "create_room", "join_room", "rejoin_room", "leave_room", "start_game",
            "submit_answer", "use_joker", "get_hint", "restart_room",
            "chat_message", "chat_clear", "chat_delete_message", "heartbeat",
            "set_presentation_mode", "update_settings", "init_teams", "draft_pick",
            "cheat_update_score", "cheat_rename_player", "cheat_set_lives",
            "cheat_add_score_all", "cheat_skip_question", "cheat_force_start",
            "cheat_reset_player", "cheat_reset_global_stats", "cheat_teleport",
            "cheat_set_infinite_lives", "reaction", "rephrase_question",
            "admin_kick_player", "admin_ban_player", "admin_force_end_game",
            "admin_take_host", "admin_transfer_host", "admin_chat_system",
            "svoyaigra_select_cell", "svoyaigra_buzz", "svoyaigra_answer",
            "svoyaigra_host_reveal",
        }
        missing = required - handlers
        assert not missing, f"missing handlers: {sorted(missing)}"


class TestRoomChatIntegration:
    def test_room_and_registry_work_together(self):
        registry = RoomRegistry()
        room = registry.create("host")
        room.add_player("host", "Host")
        assert room.players_list(viewer_sid="host")[0]["is_host"] is True
