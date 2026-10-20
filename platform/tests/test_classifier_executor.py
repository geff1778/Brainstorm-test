"""Classifier, executor parsing and error-classification tests."""

from __future__ import annotations

from ohp.classifier import classify, detect_project
from ohp.executor import backoff_delay, build_prompt, classify_error, parse_result
from ohp.models import (
    Priority,
    Project,
    RiskLevel,
    Task,
    TaskType,
)


def test_security_request_is_high_risk_and_needs_approval() -> None:
    c = classify("Audit the auth code for a secret leak in production")
    assert c.task_type is TaskType.SECURITY
    assert c.risk is RiskLevel.HIGH
    assert c.requires_approval is True
    assert c.role == "security"


def test_documentation_request_is_low_priority() -> None:
    c = classify("Update the README with setup instructions")
    assert c.task_type is TaskType.DOCUMENTATION
    assert c.priority is Priority.P3
    assert c.risk is RiskLevel.LOW


def test_high_risk_developer_task_upgrades_role_to_architect() -> None:
    c = classify("Implement a database migration for production")
    assert c.risk is RiskLevel.HIGH
    assert c.role == "architect"


def test_debugging_detected_from_keywords() -> None:
    c = classify("The endpoint crashes with a traceback")
    assert c.task_type is TaskType.DEBUGGING
    assert c.role == "developer"


def test_detect_project_matches_name_tokens() -> None:
    assert detect_project("Please work on the Brainstorm project", ["Brainstorm-test", "other"]) == "Brainstorm-test"
    assert detect_project("unrelated", ["Brainstorm-test"]) == "default"


def test_parse_result_from_fenced_json() -> None:
    text = 'here is the result\n```json\n{"status":"completed","summary":"ok","changed_files":["a.py"]}\n```'
    result = parse_result(text)
    assert result.status == "completed"
    assert result.summary == "ok"
    assert result.changed_files == ["a.py"]


def test_parse_result_falls_back_to_prose() -> None:
    result = parse_result("I could not finish because the repo is missing.")
    assert result.status == "completed"
    assert "could not finish" in result.summary


def test_parse_result_coerces_string_lists() -> None:
    result = parse_result('```json\n{"status":"completed","summary":"x","changed_files":"a.py"}\n```')
    assert result.changed_files == ["a.py"]


def test_classify_error_buckets() -> None:
    assert classify_error("connection reset by peer") == "retryable"
    assert classify_error("403 Forbidden") == "fatal"
    assert classify_error("something odd") == "unknown"


def test_backoff_is_capped() -> None:
    for attempt in range(1, 12):
        assert 0 <= backoff_delay(attempt, cap=10.0) <= 10.0


def test_build_prompt_is_self_contained() -> None:
    task = Task(
        title="Add endpoint",
        description="Expose /health",
        acceptance=["returns 200"],
        role="developer",
    )
    project = Project(name="p", repo="o/r", working_dir="/w", test_cmd="pytest")
    prompt = build_prompt(task, project)
    assert "Add endpoint" in prompt
    assert "returns 200" in prompt
    assert "pytest" in prompt
    assert '"status"' in prompt  # result contract present
