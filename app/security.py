"""Security primitives: password hashing, rate limiting and HTML escaping.

Password hashing
----------------
New passwords use PBKDF2-HMAC-SHA256 with a per-password random salt and a high
iteration count.  ``verify_password`` still accepts the legacy
``salt:sha256(salt:pwd)`` format that earlier versions of BrainStorm wrote, and
``needs_rehash`` lets callers transparently upgrade a legacy hash on next login.

Rate limiting
-------------
A tiny in-process token bucket keyed by client identity.  It is intentionally
dependency-free; for multi-worker deployments it can be swapped for Redis (see
ROADMAP.md) without touching call sites.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from html import escape

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 260_000
PBKDF2_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256.

    Returns a self-describing string: ``pbkdf2_sha256$<iterations>$<salt>$<hash>``.
    """
    salt = os.urandom(PBKDF2_SALT_BYTES).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    ).hex()
    return f"{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}${salt}${digest}"


def _legacy_hash(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()


def verify_password(password: str, stored: str) -> bool:
    """Verify ``password`` against a stored hash (new or legacy format)."""
    if not stored or ":" not in stored and "$" not in stored:
        return False

    if stored.startswith(f"{PBKDF2_ALGORITHM}$"):
        try:
            _, iterations, salt, digest = stored.split("$", 3)
            candidate = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
            ).hex()
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(candidate, digest)

    # Legacy ``<salt>:<sha256>`` format written by earlier releases.
    try:
        salt, digest = stored.split(":", 1)
    except ValueError:
        return False
    return hmac.compare_digest(_legacy_hash(password, salt), digest)


def needs_rehash(stored: str) -> bool:
    """Return ``True`` when ``stored`` should be upgraded to the current scheme."""
    return not stored.startswith(f"{PBKDF2_ALGORITHM}$")


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """In-process sliding-window rate limiter.

    Each identity (IP address, username, socket id) gets a token bucket.  The
    bucket refills continuously so a client that waits long enough is never
    permanently locked out.
    """

    def __init__(self, limit: int, window_seconds: float, *, max_keys: int = 10_000) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1.0, float(window_seconds))
        self.max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}

    def _evict_if_needed(self, now: float) -> None:
        if len(self._buckets) <= self.max_keys:
            return
        cutoff = now - self.window * 2
        for key in [k for k, b in self._buckets.items() if b.updated < cutoff]:
            self._buckets.pop(key, None)

    def allow(self, key: str, *, cost: float = 1.0) -> bool:
        """Consume ``cost`` tokens for ``key``; return ``False`` when exhausted."""
        now = time.monotonic()
        self._evict_if_needed(now)
        bucket = self._buckets.get(key)
        rate = self.limit / self.window
        if bucket is None:
            bucket = _Bucket(tokens=float(self.limit), updated=now)
            self._buckets[key] = bucket
        else:
            bucket.tokens = min(float(self.limit), bucket.tokens + (now - bucket.updated) * rate)
            bucket.updated = now
        if bucket.tokens < cost:
            return False
        bucket.tokens -= cost
        return True

    def retry_after(self, key: str) -> int:
        """Seconds until ``key`` has at least one token again."""
        bucket = self._buckets.get(key)
        if bucket is None:
            return 0
        rate = self.limit / self.window
        if bucket.tokens >= 1:
            return 0
        return max(1, int((1 - bucket.tokens) / rate))

    def reset(self, key: str | None = None) -> None:
        """Clear one bucket, or all buckets when ``key`` is ``None``."""
        if key is None:
            self._buckets.clear()
        else:
            self._buckets.pop(key, None)


def escape_html(value: object) -> str:
    """Escape a value for safe interpolation into HTML."""
    return escape("" if value is None else str(value), quote=True)


def client_ip(remote_addr: str | None, forwarded_for: str | None, *, trust_proxy: bool) -> str:
    """Resolve the effective client IP.

    ``X-Forwarded-For`` is only honoured when the deployment explicitly opts in
    via ``TRUST_PROXY=true``; otherwise the header is trivially spoofable.
    """
    if trust_proxy and forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return remote_addr or "unknown"


def safe_equal(candidate: str | None, expected: str | None) -> bool:
    """Constant-time string comparison for secrets."""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(str(candidate), str(expected))
