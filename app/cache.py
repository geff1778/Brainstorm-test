"""Thread-safe TTL + LRU cache.

The previous implementation stored generated question sets in a plain dict with
no eviction and no locking, so a long-running process leaked memory forever and
concurrent writers could interleave.  This implementation bounds the number of
entries, expires them after a TTL and uses a re-entrant lock.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    """Bounded cache with per-entry time-to-live and LRU eviction."""

    def __init__(self, max_entries: int = 256, ttl_seconds: float = 3600.0) -> None:
        self.max_entries = max(1, int(max_entries))
        self.ttl = float(ttl_seconds)
        self._store: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: Any) -> Any | None:
        """Return the cached value or ``None`` when missing/expired."""
        now = time.time()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            timestamp, value = entry
            if now - timestamp >= self.ttl:
                del self._store[key]
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: Any, value: Any) -> None:
        """Store ``value`` under ``key``, evicting the oldest entry if needed."""
        with self._lock:
            self._store[key] = (time.time(), value)
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)
                self.evictions += 1

    def invalidate(self, key: Any) -> None:
        """Remove a single entry."""
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        """Remove every entry."""
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def stats(self) -> dict:
        """Return counters useful for monitoring."""
        with self._lock:
            return {
                "size": len(self._store),
                "max_entries": self.max_entries,
                "ttl": self.ttl,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
            }


#: Shared question cache: ``(topic, count, difficulty, num_options) -> questions``.
question_cache = TTLCache(max_entries=256, ttl_seconds=3600)
