"""Room registry.

Owns the live ``Room`` objects and the ``sid -> room code`` reverse index that
makes player lookup O(1) instead of scanning every room.  A lock guards mutation
so the periodic cleanup task and socket handlers cannot corrupt the index.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

from ..config import settings
from ..logging_setup import get_logger
from .models import ROOM_EMPTY_TIMEOUT, ROOM_IDLE_TIMEOUT, Room, gen_code

logger = get_logger(__name__)


class RoomRegistry:
    """Thread-safe container for active rooms."""

    def __init__(self, *, idle_timeout: int = ROOM_IDLE_TIMEOUT,
                 empty_timeout: int = ROOM_EMPTY_TIMEOUT,
                 max_rooms: int = 2000) -> None:
        self._rooms: dict[str, Room] = {}
        self._sid_room: dict[str, str] = {}
        self._lock = threading.RLock()
        self.idle_timeout = idle_timeout
        self.empty_timeout = empty_timeout
        self.max_rooms = max_rooms

    # ── Access ───────────────────────────────────────────────
    def get(self, code: str) -> Room | None:
        """Return a room by code."""
        if not code:
            return None
        with self._lock:
            return self._rooms.get(code.upper())

    def get_by_sid(self, sid: str) -> Room | None:
        """Return the room a socket id currently belongs to."""
        with self._lock:
            code = self._sid_room.get(sid)
            return self._rooms.get(code) if code else None

    def exists(self, code: str) -> bool:
        """Return ``True`` when the room code is live."""
        with self._lock:
            return code.upper() in self._rooms

    def codes(self) -> list[str]:
        """Snapshot of all live room codes."""
        with self._lock:
            return list(self._rooms.keys())

    def values(self) -> list[Room]:
        """Snapshot of all live rooms."""
        with self._lock:
            return list(self._rooms.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._rooms)

    def __iter__(self) -> Iterator[Room]:
        return iter(self.values())

    def at_capacity(self) -> bool:
        """Return ``True`` when no new rooms may be created."""
        with self._lock:
            return len(self._rooms) >= self.max_rooms

    # ── Mutation ─────────────────────────────────────────────
    def create(self, host_sid: str, *, is_public: bool = False, is_sandbox: bool = False) -> Room:
        """Create a new room and index its host."""
        with self._lock:
            code = gen_code(self._rooms)
            room = Room(code=code, host_sid=host_sid, is_public=is_public, is_sandbox=is_sandbox)
            self._rooms[code] = room
            self._sid_room[host_sid] = code
            return room

    def index(self, sid: str, code: str) -> None:
        """Bind a socket id to a room code."""
        with self._lock:
            self._sid_room[sid] = code.upper()

    def unindex(self, sid: str) -> None:
        """Drop a socket id from the reverse index."""
        with self._lock:
            self._sid_room.pop(sid, None)

    def rebind(self, old_sid: str, new_sid: str, code: str) -> None:
        """Move the reverse-index entry when a player reconnects with a new sid."""
        with self._lock:
            self._sid_room.pop(old_sid, None)
            self._sid_room[new_sid] = code.upper()

    def remove(self, code: str) -> Room | None:
        """Remove a room and every sid index that pointed at it."""
        with self._lock:
            room = self._rooms.pop(code.upper(), None)
            if room is None:
                return None
            for sid in list(room.players.keys()):
                if self._sid_room.get(sid) == code.upper():
                    self._sid_room.pop(sid, None)
            return room

    def cleanup_stale(self) -> int:
        """Drop empty/idle rooms.  Returns the number removed."""
        now = time.time()
        with self._lock:
            stale = []
            for code, room in self._rooms.items():
                age = now - room.last_activity
                if not room.players:
                    if age > self.empty_timeout:
                        stale.append(code)
                elif room.state == "waiting" and age > self.idle_timeout:
                    stale.append(code)
            for code in stale:
                room = self._rooms.pop(code, None)
                if room:
                    for sid in list(room.players.keys()):
                        if self._sid_room.get(sid) == code:
                            self._sid_room.pop(sid, None)
        if stale:
            logger.info("Cleaned up %d stale rooms", len(stale))
        return len(stale)

    def clear(self) -> None:
        """Drop every room (used by tests)."""
        with self._lock:
            self._rooms.clear()
            self._sid_room.clear()


#: Process-wide registry instance.
registry = RoomRegistry(
    idle_timeout=settings.room_idle_timeout,
    empty_timeout=settings.room_empty_timeout,
    max_rooms=settings.max_rooms,
)
