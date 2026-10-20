"""The task lifecycle finite-state machine.

Transitions are declared explicitly and validated on every change so the store
can never be driven into an inconsistent state. Each transition records who may
perform it, the event that triggers it, and whether it is idempotent (safe to
apply when the task is *already* in the target state).
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import TaskStatus

S = TaskStatus


@dataclass(frozen=True)
class Transition:
    """A single legal edge in the state machine."""

    source: TaskStatus
    target: TaskStatus
    trigger: str
    actors: frozenset[str] = frozenset({"dispatcher", "executor", "operator"})
    idempotent: bool = True


def _t(
    source: TaskStatus,
    target: TaskStatus,
    trigger: str,
    *,
    actors: tuple[str, ...] = ("dispatcher", "executor", "operator"),
    idempotent: bool = True,
) -> Transition:
    return Transition(source, target, trigger, frozenset(actors), idempotent)


# The complete transition table. Anything not listed here is illegal.
TRANSITIONS: tuple[Transition, ...] = (
    # --- admission ---
    _t(S.CREATED, S.QUEUED, "enqueue"),
    _t(S.CREATED, S.WAITING_FOR_APPROVAL, "require_approval", actors=("dispatcher",)),
    _t(S.CREATED, S.BLOCKED, "block"),
    _t(S.CREATED, S.CANCELLED, "cancel"),
    # --- approval gate ---
    _t(S.WAITING_FOR_APPROVAL, S.QUEUED, "approve", actors=("operator", "dispatcher")),
    _t(S.WAITING_FOR_APPROVAL, S.CANCELLED, "cancel", actors=("operator",)),
    # --- dependencies ---
    _t(S.BLOCKED, S.QUEUED, "unblock"),
    _t(S.BLOCKED, S.CANCELLED, "cancel"),
    # --- execution ---
    _t(S.QUEUED, S.RUNNING, "start"),
    _t(S.QUEUED, S.CANCELLED, "cancel"),
    _t(S.RUNNING, S.WAITING_FOR_AGENT, "await_agent"),
    _t(S.WAITING_FOR_AGENT, S.RUNNING, "agent_ready"),
    _t(S.RUNNING, S.COMPLETED, "complete", actors=("executor", "dispatcher", "operator")),
    _t(S.RUNNING, S.FAILED, "fail", actors=("executor", "dispatcher", "operator")),
    _t(S.RUNNING, S.RETRY_WAIT, "schedule_retry"),
    _t(S.RUNNING, S.BLOCKED, "block"),
    _t(S.RUNNING, S.OUTCOME_UNKNOWN, "outcome_unknown"),
    _t(S.RUNNING, S.CANCELLED, "cancel"),
    # --- recovery ---
    _t(S.RETRY_WAIT, S.QUEUED, "retry_ready"),
    _t(S.RETRY_WAIT, S.FAILED, "exhaust_retries", actors=("dispatcher", "operator")),
    _t(S.RETRY_WAIT, S.CANCELLED, "cancel"),
    _t(S.OUTCOME_UNKNOWN, S.RUNNING, "reconcile_running", actors=("dispatcher", "operator")),
    _t(S.OUTCOME_UNKNOWN, S.COMPLETED, "reconcile_completed", actors=("dispatcher", "operator")),
    _t(S.OUTCOME_UNKNOWN, S.FAILED, "reconcile_failed", actors=("dispatcher", "operator")),
    _t(S.OUTCOME_UNKNOWN, S.QUEUED, "reconcile_requeue", actors=("dispatcher", "operator")),
)

# Index for O(1) lookup: (source, target) -> Transition
_BY_EDGE: dict[tuple[TaskStatus, TaskStatus], Transition] = {
    (t.source, t.target): t for t in TRANSITIONS
}


class IllegalTransition(RuntimeError):
    """Raised when a caller attempts a transition not in the table."""


def get_transition(source: TaskStatus, target: TaskStatus) -> Transition | None:
    """Return the transition for ``source -> target`` or ``None``."""
    return _BY_EDGE.get((source, target))


def can_transition(source: TaskStatus, target: TaskStatus) -> bool:
    return (source, target) in _BY_EDGE


def validate_transition(source: TaskStatus, target: TaskStatus) -> Transition:
    """Return the transition, raising :class:`IllegalTransition` if absent."""
    transition = _BY_EDGE.get((source, target))
    if transition is None:
        raise IllegalTransition(f"{source.value} -> {target.value} is not allowed")
    return transition


def allowed_targets(source: TaskStatus) -> list[TaskStatus]:
    """All states reachable from ``source`` in one step."""
    return [t.target for t in TRANSITIONS if t.source is source]
