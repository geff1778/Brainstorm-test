"""Per-room in-memory chat log.

Bounded so a long game cannot grow memory without limit, and dropped together
with the room by :class:`~app.domain.registry.RoomRegistry`.
"""

from __future__ import annotations

import threading
import time

MAX_MESSAGES = 100
MAX_MESSAGE_LEN = 200


class ChatStore:
    """Thread-safe, bounded chat history keyed by room code."""

    def __init__(self, max_messages: int = MAX_MESSAGES) -> None:
        self.max_messages = max_messages
        self._logs: dict[str, list[dict]] = {}
        self._lock = threading.RLock()

    def history(self, code: str) -> list[dict]:
        """Return a copy of the message list for ``code``."""
        with self._lock:
            return list(self._logs.get(code, []))

    def append(self, code: str, *, name: str, text: str, is_system: bool = False,
               is_host: bool = False) -> dict:
        """Append a message and return the stored record."""
        record = {
            "name": name,
            "text": text[:MAX_MESSAGE_LEN],
            "ts": time.time(),
            "is_system": is_system,
            "is_host": is_host,
        }
        with self._lock:
            log = self._logs.setdefault(code, [])
            log.append(record)
            if len(log) > self.max_messages:
                del log[:-self.max_messages]
        return record

    def delete(self, code: str, ts: float) -> bool:
        """Delete a message by timestamp; return ``True`` when one was removed."""
        with self._lock:
            log = self._logs.get(code)
            if not log:
                return False
            for index, message in enumerate(log):
                if abs(float(message.get("ts", 0)) - float(ts)) < 0.001:
                    del log[index]
                    return True
        return False

    def clear(self, code: str) -> None:
        """Drop the history for one room."""
        with self._lock:
            self._logs.pop(code, None)

    def drop(self, code: str) -> None:
        """Alias for :meth:`clear` used when a room is destroyed."""
        self.clear(code)


chat_store = ChatStore()
