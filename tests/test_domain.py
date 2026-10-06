"""Tests for the pure game domain models."""

from __future__ import annotations

import time

from app.domain.models import JOKER_COST, Room, gen_code


def make_room(mode="classic", players=("s1", "s2")):
    room = Room(code="TESTRM", host_sid=players[0])
    for sid in players:
        room.add_player(sid, f"P{sid}")
    room.settings["game_mode"] = mode
    room.questions = [
        {"question": "Q1?", "options": ["a", "b", "c", "d"], "correct": 1, "bonus": False},
        {"question": "Q2?", "options": ["a", "b", "c", "d"], "correct": 2, "bonus": False},
    ]
    room.current_q = 0
    room.q_start_time = time.time()
    return room


class TestRoomBasics:
    def test_gen_code_is_six_uppercase_letters(self):
        code = gen_code(set())
        assert len(code) == 6 and code.isupper() and code.isalpha()

    def test_gen_code_avoids_collisions(self):
        assert gen_code({"AAAAAA"}) != "AAAAAA"

    def test_name_taken_is_case_insensitive(self):
        room = make_room()
        assert room.name_taken("ps1")
        assert not room.name_taken("Ps1", exclude_sid="s1")

    def test_remove_player_detaches_from_teams(self):
        room = make_room()
        room.init_teams(2)
        room.teams[1].members.append("s1")
        room.teams[1].leader_sid = "s1"
        room.remove_player("s1")
        assert "s1" not in room.teams[1].members
        assert room.teams[1].leader_sid is None

    def test_reassign_host_promotes_next_player(self):
        room = make_room()
        room.players["s2"].join_time = time.time() + 1
        room.remove_player("s1")
        new_host = room.reassign_host()
        assert new_host is not None and new_host.sid == "s2"


class TestTeams:
    def test_init_teams_clamps_count(self):
        room = make_room()
        room.init_teams(99)
        assert len(room.teams) == 7

    def test_auto_assignment_is_even(self):
        room = make_room(players=("a", "b", "c", "d"))
        room.init_teams(2)
        room.assign_teams_auto()
        sizes = sorted(len(t.members) for t in room.teams.values())
        assert sizes == [2, 2]

    def test_team_scores_aggregate(self):
        room = make_room(mode="team", players=("a", "b"))
        room.init_teams(2)
        room.players["a"].team = 1
        room.players["a"].score = 100
        room.players["b"].team = 2
        room.players["b"].score = 50
        assert room.team_scores() == {1: 100, 2: 50}


class TestScoring:
    def test_correct_answer_awards_points(self):
        room = make_room(players=("s1",))
        room.players["s1"].answer_time = time.time()
        points = room.award_point("s1")
        assert points > 0 and room.players["s1"].score == points

    def test_bonus_multiplies_score(self):
        room = make_room(players=("s1",))
        room.questions[0]["bonus"] = True
        room.players["s1"].answer_time = time.time()
        room.award_point("s1")
        bonus_score = room.players["s1"].score

        room2 = make_room(players=("s1",))
        room2.players["s1"].answer_time = time.time()
        room2.award_point("s1")
        assert bonus_score > room2.players["s1"].score

    def test_streak_adds_bonus(self):
        room = make_room(players=("s1",))
        room.players["s1"].streak = 5
        room.players["s1"].answer_time = time.time()
        with_streak = room.award_point("s1")
        room2 = make_room(players=("s1",))
        room2.players["s1"].answer_time = time.time()
        assert with_streak > room2.award_point("s1")

    def test_coop_mode_scores_everyone(self):
        room = make_room(mode="coop", players=("s1", "s2"))
        room.players["s1"].answer_time = time.time()
        points = room.award_point("s1")
        assert room.players["s2"].score == points

    def test_late_answer_still_scores_base(self):
        room = make_room(players=("s1",))
        room.q_start_time = time.time() - 1000
        room.players["s1"].answer_time = time.time()
        assert room.award_point("s1") >= 100


class TestLivesAndJoker:
    def test_lose_life_eliminates_at_zero(self):
        room = make_room(mode="lives", players=("s1",))
        for _ in range(3):
            room.lose_life("s1")
        assert room.players["s1"].is_spectator

    def test_infinite_lives_never_decrease(self):
        room = make_room(mode="lives", players=("s1",))
        room.players["s1"].infinite_lives = True
        assert room.lose_life("s1") == 3

    def test_joker_requires_points(self):
        room = make_room(players=("s1",))
        room.players["s1"].score = JOKER_COST - 1
        assert room.use_joker("s1") is None

    def test_joker_returns_correct_and_one_wrong(self):
        room = make_room(players=("s1",))
        room.players["s1"].score = JOKER_COST
        keep = room.use_joker("s1")
        assert keep is not None
        assert 1 in keep and len(keep) == 2
        assert room.players["s1"].score == 0

    def test_joker_cannot_be_used_twice(self):
        room = make_room(players=("s1",))
        room.players["s1"].score = JOKER_COST * 2
        room.use_joker("s1")
        assert room.use_joker("s1") is None


class TestAdaptiveDifficulty:
    def test_high_accuracy_raises_difficulty(self):
        room = make_room()
        room.current_difficulty = "medium"
        for _ in range(8):
            room.record_answer_stat(True)
        assert room.recalculate_difficulty() == "hard"

    def test_low_accuracy_lowers_difficulty(self):
        room = make_room()
        room.current_difficulty = "medium"
        for _ in range(8):
            room.record_answer_stat(False)
        assert room.recalculate_difficulty() == "easy"

    def test_insufficient_samples_keep_difficulty(self):
        room = make_room()
        room.current_difficulty = "hard"
        room.record_answer_stat(True)
        assert room.recalculate_difficulty() == "hard"

    def test_unknown_difficulty_never_crashes(self):
        room = make_room()
        room.current_difficulty = "impossible"
        for _ in range(8):
            room.record_answer_stat(True)
        assert room.recalculate_difficulty() in ("easy", "medium", "hard")


class TestQuestionFlow:
    def test_all_answered_requires_every_player(self):
        room = make_room()
        room.players["s1"].answered = True
        assert room.all_answered() is False
        room.players["s2"].answered = True
        assert room.all_answered() is True

    def test_advance_finishes_at_end(self):
        room = make_room(players=("s1",))
        assert room.advance_question() is True
        assert room.advance_question() is False
        assert room.state == "finished"

    def test_final_results_are_sorted_by_score(self):
        room = make_room(players=("s1", "s2"))
        room.players["s1"].score = 10
        room.players["s2"].score = 50
        results = room.final_results()
        assert results["players"][0]["name"] == "Ps2"

    def test_reset_for_restart_clears_state(self):
        room = make_room(players=("s1",))
        room.players["s1"].score = 100
        room.state = "finished"
        room.reset_for_restart(keep_scores=False)
        assert room.state == "waiting"
        assert room.players["s1"].score == 0
        assert room.questions == []
