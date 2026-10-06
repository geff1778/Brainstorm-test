"""Tests for the validation helpers."""

from __future__ import annotations

import pytest

from app.errors import ValidationError
from app.validation import (
    as_bool,
    as_int,
    as_str,
    clamp,
    validate_difficulty,
    validate_mode,
    validate_options,
    validate_url,
    validate_username,
)


class TestAsStr:
    def test_strips_and_truncates(self):
        assert as_str("  hi  ") == "hi"
        assert len(as_str("x" * 500, max_len=10)) == 10

    def test_non_string_is_coerced(self):
        assert as_str(42) == "42"

    def test_empty_becomes_default(self):
        assert as_str(None, default="fallback") == "fallback"


class TestAsInt:
    def test_valid(self):
        assert as_int("7") == 7

    def test_clamping(self):
        assert as_int(100, maximum=10) == 10
        assert as_int(-5, minimum=0) == 0

    def test_invalid_uses_default(self):
        assert as_int("abc", default=3) == 3

    def test_invalid_without_default_raises(self):
        with pytest.raises(ValidationError):
            as_int("abc")


class TestAsBool:
    @pytest.mark.parametrize("value", [True, 1, "1", "true", "yes", "on"])
    def test_truthy(self, value):
        assert as_bool(value) is True

    @pytest.mark.parametrize("value", [False, 0, "", "no", None])
    def test_falsy(self, value):
        assert as_bool(value) is False


class TestClamp:
    def test_bounds(self):
        assert clamp(5, 0, 10) == 5
        assert clamp(-1, 0, 10) == 0
        assert clamp(99, 0, 10) == 10


class TestValidators:
    def test_validate_username_accepts_unicode(self):
        assert validate_username("Игрок_1") == "Игрок_1"

    def test_validate_username_rejects_empty(self):
        with pytest.raises(ValidationError):
            validate_username("")

    def test_validate_difficulty_accepts_known(self):
        assert validate_difficulty("hard") == "hard"

    def test_validate_difficulty_falls_back_to_default(self):
        assert validate_difficulty("impossible") == "medium"

    def test_validate_mode_accepts_known(self):
        assert validate_mode("team") == "team"

    def test_validate_mode_falls_back_to_default(self):
        assert validate_mode("battle-royale") == "classic"

    def test_validate_options_requires_minimum(self):
        with pytest.raises(ValidationError):
            validate_options(["only one"])

    def test_validate_options_rejects_duplicates(self):
        with pytest.raises(ValidationError):
            validate_options(["a", "a"])

    def test_validate_options_accepts_valid(self):
        assert validate_options(["a", "b", "c"]) == ["a", "b", "c"]

    @pytest.mark.parametrize("url", ["ftp://x", "javascript:alert(1)", "not-a-url"])
    def test_validate_url_rejects_bad(self, url):
        with pytest.raises(ValidationError):
            validate_url(url)

    def test_validate_url_accepts_http(self):
        assert validate_url("https://example.com/page") == "https://example.com/page"
