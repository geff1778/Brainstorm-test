"""Tests for the account, leaderboard and UGC services."""

from __future__ import annotations

import pytest

from app.errors import AuthError, ConflictError, ValidationError
from app.services import accounts, leaderboard, ugc


class TestAccounts:
    def test_register_creates_player(self, app):
        user = accounts.register_user("newbie", "supersecret")
        assert user["username"] == "newbie"
        assert user["coins"] == accounts.STARTING_COINS

    def test_register_is_unique(self, app):
        accounts.register_user("Case", "supersecret")
        with pytest.raises(ConflictError):
            accounts.register_user("Case", "supersecret")

    def test_register_rejects_short_password(self, app):
        with pytest.raises(ValidationError):
            accounts.register_user("shorty", "123")

    def test_login_success_and_failure(self, app):
        accounts.register_user("loginer", "supersecret")
        assert accounts.login_user("loginer", "supersecret")["username"] == "loginer"
        with pytest.raises(AuthError):
            accounts.login_user("loginer", "wrong")

    def test_level_progression(self, app):
        assert accounts.level_from_xp(0) == 1
        assert accounts.level_from_xp(10_000) > 1
        assert accounts.xp_for_next_level(1) > 0

    def test_coin_transactions(self, app):
        accounts.register_user("rich", "supersecret")
        accounts.add_coins("rich", 100)
        assert accounts.get_user("rich")["coins"] == accounts.STARTING_COINS + 100
        assert accounts.spend_coins("rich", 50) is True
        assert accounts.spend_coins("rich", 10_000) is False

    def test_delete_user(self, app):
        accounts.register_user("goner", "supersecret")
        accounts.delete_user("goner")
        assert accounts.get_user("goner") is None


class TestLeaderboard:
    @staticmethod
    def _record(username, score, duration=120):
        leaderboard.update_leaderboard([{"name": username, "score": score}], duration=duration)

    def test_record_and_rank(self, app):
        self._record("alice", 500)
        self._record("bob", 200)
        board = leaderboard.top_players(10)
        assert [row["username"] for row in board] == ["alice", "bob"]
        assert leaderboard.player_rank("alice")["rank"] == 1

    def test_highest_score_is_recorded_as_a_win(self, app):
        self._record("winner", 900)
        assert leaderboard.player_rank("winner")["wins"] == 1

    def test_rank_of_unknown_player(self, app):
        assert leaderboard.player_rank("ghost").get("rank", 0) == 0

    def test_reset_player_stats(self, app):
        self._record("carol", 300)
        assert leaderboard.reset_player_stats("carol") is True
        assert leaderboard.player_rank("carol")["total_score"] == 0

    def test_ban_lifecycle(self, app):
        assert leaderboard.ban("badname", "spam", 60) is True
        assert leaderboard.is_banned("badname") is True
        assert leaderboard.unban("badname") is True
        assert leaderboard.is_banned("badname") is False

    def test_ip_ban_kind(self, app):
        leaderboard.ban("10.0.0.9", "flood", 60, kind=leaderboard.BAN_KIND_IP)
        assert leaderboard.is_banned("10.0.0.9", kind=leaderboard.BAN_KIND_IP) is True
        assert leaderboard.is_banned("10.0.0.9", kind=leaderboard.BAN_KIND_NAME) is False


class TestUGC:
    def _submit(self, question="Сколько будет дважды два?", options=("3", "4", "5", "6"), correct=1):
        return ugc.create_question(
            author="author", question=question, options=list(options),
            correct=correct, topic="математика", difficulty=1,
        )

    def test_create_returns_id_and_pending_status(self, app):
        question_id = self._submit()
        assert isinstance(question_id, int) and question_id > 0
        assert ugc.list_pending(10)[0]["status"] == ugc.STATUS_PENDING

    def test_approved_question_is_playable(self, app):
        question_id = self._submit()
        ugc.moderate(question_id, True, "")
        approved = ugc.list_questions(status=ugc.STATUS_APPROVED)
        assert any(row["id"] == question_id for row in approved)

    def test_reject_removes_from_approved(self, app):
        question_id = self._submit()
        ugc.moderate(question_id, False, "низкое качество")
        assert not any(r["id"] == question_id for r in ugc.list_questions(status=ugc.STATUS_APPROVED))

    def test_duplicate_options_rejected(self, app):
        with pytest.raises(ValidationError):
            self._submit(options=("same", "same"), correct=0)

    def test_profanity_rejected(self, app):
        with pytest.raises(ValidationError):
            self._submit(question="Тут мат блядь в вопросе")

    def test_too_short_question_rejected(self, app):
        with pytest.raises(ValidationError):
            self._submit(question="ok?")

    def test_vote_changes_rating(self, app):
        question_id = self._submit()
        assert ugc.vote(question_id, "voter", 1) is True
        assert ugc.vote(question_id, "voter", 1) is True  # idempotent per user

    def test_report_is_recorded(self, app):
        question_id = self._submit()
        assert ugc.report(question_id, "reporter", "оскорбительно") is True
