"""Tests for security primitives: password hashing and rate limiting."""

from __future__ import annotations

from app.security import (
    RateLimiter,
    client_ip,
    escape_html,
    hash_password,
    needs_rehash,
    safe_equal,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_and_verify_roundtrip(self):
        stored = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", stored)

    def test_wrong_password_is_rejected(self):
        stored = hash_password("secret-password")
        assert not verify_password("other-password", stored)

    def test_hashes_use_unique_salts(self):
        assert hash_password("same") != hash_password("same")

    def test_hash_is_self_describing(self):
        assert hash_password("x").startswith("pbkdf2_sha256$")

    def test_legacy_sha256_hash_still_verifies(self):
        import hashlib
        salt = "deadbeef"
        legacy = f"{salt}:{hashlib.sha256(f'{salt}:oldpass'.encode()).hexdigest()}"
        assert verify_password("oldpass", legacy)
        assert needs_rehash(legacy) is True

    def test_new_hash_does_not_need_rehash(self):
        assert needs_rehash(hash_password("x")) is False

    def test_garbage_hash_is_rejected(self):
        assert not verify_password("x", "not-a-hash")


class TestRateLimiter:
    def test_allows_up_to_limit(self):
        limiter = RateLimiter(limit=3, window_seconds=60)
        assert all(limiter.allow("client") for _ in range(3))
        assert limiter.allow("client") is False

    def test_buckets_are_isolated_per_key(self):
        limiter = RateLimiter(limit=1, window_seconds=60)
        assert limiter.allow("a")
        assert limiter.allow("b")

    def test_retry_after_is_positive_when_exhausted(self):
        limiter = RateLimiter(limit=1, window_seconds=60)
        limiter.allow("c")
        assert limiter.retry_after("c") >= 1

    def test_reset_clears_state(self):
        limiter = RateLimiter(limit=1, window_seconds=60)
        limiter.allow("d")
        limiter.reset("d")
        assert limiter.allow("d")


class TestHelpers:
    def test_escape_html_neutralises_tags(self):
        assert escape_html("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"

    def test_safe_equal_matches_identical(self):
        assert safe_equal("abc", "abc")
        assert not safe_equal("abc", "abd")
        assert not safe_equal(None, "abc")

    def test_client_ip_ignores_forwarded_header_by_default(self):
        assert client_ip("10.0.0.1", "1.2.3.4", trust_proxy=False) == "10.0.0.1"

    def test_client_ip_honours_forwarded_header_when_trusted(self):
        assert client_ip("10.0.0.1", "1.2.3.4, 5.6.7.8", trust_proxy=True) == "1.2.3.4"
