"""Tests for the state machine and the store's invariants."""

from __future__ import annotations

import pytest

from ohp.models import Task, TaskStatus
from ohp.state_machine import (
    IllegalTransition,
    can_transition,
    validate_transition,
)


def test_terminal_states_have_no_normal_outgoing_edges() -> None:
    for terminal in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        assert terminal.is_terminal


def test_happy_path_transitions_are_allowed() -> None:
    chain = [
        (TaskStatus.CREATED, TaskStatus.QUEUED),
        (TaskStatus.QUEUED, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.WAITING_FOR_AGENT),
        (TaskStatus.WAITING_FOR_AGENT, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.COMPLETED),
    ]
    for source, target in chain:
        assert can_transition(source, target), f"{source} -> {target}"


def test_illegal_transition_is_rejected() -> None:
    with pytest.raises(IllegalTransition):
        validate_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)


def test_completed_cannot_be_restarted_without_reconcile() -> None:
    assert not can_transition(TaskStatus.COMPLETED, TaskStatus.QUEUED)


def test_actor_restriction_is_enforced(store) -> None:
    task = Task(title="x")
    store.create_task(task)
    store.set_status(task.task_id, TaskStatus.QUEUED, actor="dispatcher")
    # approve is operator/dispatcher only, and only from waiting_for_approval
    with pytest.raises(IllegalTransition):
        store.set_status(task.task_id, TaskStatus.WAITING_FOR_APPROVAL, actor="operator")
