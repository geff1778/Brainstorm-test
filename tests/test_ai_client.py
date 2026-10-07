"""Tests for the AI question-generation service.

These lock in the fixes for the bugs found during the audit:

* batch 1-based → 0-based conversion must not corrupt valid 0-based answers;
* the fallback bank must honour ``used_questions``;
* questions must be deduplicated without a process-wide hash set.
"""

from __future__ import annotations

import pytest

from app.services import ai_client


class TestCorrectIndex:
    """``normalise_correct_index`` accepts many encodings."""

    @pytest.mark.parametrize("raw,expected", [(0, 0), (1, 1), (2, 2), (3, 3)])
    def test_zero_based_ints_are_preserved(self, raw, expected):
        question = {"correct": raw, "options": ["a", "b", "c", "d"]}
        assert ai_client.normalise_correct_index(question, 4) == expected

    def test_one_based_max_is_converted(self):
        question = {"correct": 4, "options": ["a", "b", "c", "d"]}
        assert ai_client.normalise_correct_index(question, 4) == 3

    def test_numeric_string(self):
        assert ai_client.normalise_correct_index({"correct": "2"}, 4) == 2

    def test_option_text_is_matched(self):
        question = {"correct": "Berlin", "options": ["Paris", "Berlin", "Rome", "Oslo"]}
        assert ai_client.normalise_correct_index(question, 4) == 1

    def test_letter_is_converted(self):
        assert ai_client.normalise_correct_index({"correct": "C"}, 4) == 2

    def test_out_of_range_falls_back_to_zero(self):
        assert ai_client.normalise_correct_index({"correct": 99}, 4) == 0


class TestBatchIndexConversion:
    """Regression tests for the corrupted-answer bug."""

    def test_mixed_zero_based_batch_is_untouched(self):
        questions = [
            {"question": "q1", "options": ["a", "b", "c", "d"], "correct": 1},
            {"question": "q2", "options": ["a", "b", "c", "d"], "correct": 2},
            {"question": "q3", "options": ["a", "b", "c", "d"], "correct": 3},
        ]
        ai_client._convert_batch_indices(questions, 4)
        assert [q["correct"] for q in questions] == [1, 2, 3], "0-based answers must not shift"

    def test_clear_one_based_batch_is_converted(self):
        questions = [
            {"question": "q1", "options": ["a", "b", "c", "d"], "correct": 1},
            {"question": "q2", "options": ["a", "b", "c", "d"], "correct": 4},
        ]
        ai_client._convert_batch_indices(questions, 4)
        assert [q["correct"] for q in questions] == [0, 3]

    def test_batch_without_zero_but_max_below_n_is_untouched(self):
        questions = [{"correct": 1}, {"correct": 2}]
        ai_client._convert_batch_indices(questions, 4)
        assert [q["correct"] for q in questions] == [1, 2]


class TestNormalisation:
    def test_short_question_is_rejected(self):
        assert ai_client.normalise_question({"question": "hi", "options": ["a", "b"]}, 4) is None

    def test_options_are_padded_and_deduplicated(self):
        question = {"question": "A valid question here?", "options": ["same", "same"], "correct": 0}
        result = ai_client.normalise_question(question, 4)
        assert len(result["options"]) == 4
        assert len({option.lower() for option in result["options"]}) == 4

    def test_correct_index_is_clamped(self):
        question = {"question": "A valid question here?", "options": ["a", "b"], "correct": 9}
        result = ai_client.normalise_question(question, 2)
        assert 0 <= result["correct"] < 2

    def test_meta_option_is_rejected(self):
        question = {"question": "Какой ответ верный?",
                    "options": ["Первый", "Все перечисленные", "Второй"], "correct": 0}
        assert ai_client.normalise_question(question, 3) is None

    def test_none_of_the_above_is_rejected(self):
        question = {"question": "Какой ответ верный?",
                    "options": ["Первый", "Ничего из перечисленного", "Второй"], "correct": 0}
        assert ai_client.normalise_question(question, 3) is None

    def test_long_explanation_is_truncated(self):
        question = {"question": "A valid question here?", "options": ["a", "b"],
                    "correct": 0, "explanation": "x" * 1000}
        result = ai_client.normalise_question(question, 2)
        assert len(result["explanation"]) == 300


class TestPrompt:
    def test_prompt_pins_count_and_index_range(self):
        prompt = ai_client.build_prompt("История", 7, "hard", 4)
        assert "7" in prompt
        assert "0..3" in prompt
        assert "История" in prompt

    def test_prompt_includes_example_json(self):
        prompt = ai_client.build_prompt("Космос", 3, "easy", 4)
        assert '"questions"' in prompt and '"correct"' in prompt

    def test_prompt_mentions_used_hashes(self):
        prompt = ai_client.build_prompt("тема", 2, "medium", 4, used_hashes=["abcdef123456"])
        assert "abcdef123456" in prompt


class TestParsing:
    def test_parses_markdown_fenced_json(self):
        raw = ('```json\n{"questions":[{"question":"Какой сейчас год?","options":["2024","2025"],'
               '"correct":0}]}\n```')
        parsed = ai_client.parse_questions_json(raw, num_options=2)
        assert len(parsed) == 1 and parsed[0]["correct"] == 0

    def test_parses_bare_object(self):
        raw = '{"question":"Сколько будет два плюс два?","options":["3","4"],"correct":1}'
        parsed = ai_client.parse_questions_json(raw, num_options=2)
        assert len(parsed) == 1

    def test_trailing_commas_are_tolerated(self):
        raw = '{"questions":[{"question":"Вопрос с запятой в конце?","options":["a","b"],"correct":0,},]}'
        parsed = ai_client.parse_questions_json(raw, num_options=2)
        assert len(parsed) == 1

    def test_regex_fallback_recovers_objects(self):
        raw = 'noise {"question":"Восстановленный вопрос здесь?","options":["a","b"],"correct":1} noise'
        parsed = ai_client.parse_questions_json(raw, num_options=2)
        assert len(parsed) == 1 and parsed[0]["correct"] == 1


class TestFallbackBank:
    def test_fallback_returns_requested_count(self):
        questions = ai_client.generate_questions("любая тема", 5, "medium", 4)
        assert len(questions) == 5
        for question in questions:
            assert 0 <= question["correct"] < len(question["options"])
            assert "bonus" in question

    def test_fallback_avoids_used_questions(self):
        first = ai_client.generate_questions("тема", 4, "easy", 4)
        second = ai_client.generate_questions("тема", 4, "easy", 4, used_questions=first)
        first_hashes = {ai_client.question_hash(q) for q in first}
        second_hashes = {ai_client.question_hash(q) for q in second}
        assert not (first_hashes & second_hashes), "fallback must not repeat used questions"

    def test_deduplication_is_scoped_per_call(self):
        ai_client.generate_questions("тема", 3, "easy", 4)
        second = ai_client.generate_questions("тема", 3, "easy", 4)
        # Without a shared global set, the second call is free to return questions.
        assert len(second) == 3

    def test_no_global_hash_leak_between_calls(self):
        assert not hasattr(ai_client, "_session_hashes"), "the global hash set must be gone"


class TestBackendReporting:
    def test_active_backend_without_credentials(self):
        assert "Fallback" in ai_client.active_backend()
