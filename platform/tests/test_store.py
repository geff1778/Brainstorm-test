"""Idempotency, leasing and concurrency guarantees of the store."""

from __future__ import annotations

from ohp.models import Task, TaskStatus, make_idempotency_key
from ohp.store import Store


def test_same_idempotency_key_does_not_create_duplicate(store: Store) -> None:
    key = make_idempotency_key("fix login", "proj", "debugging")
    first, created1 = store.create_task(Task(title="fix login", idempotency_key=key))
    second, created2 = store.create_task(Task(title="fix login", idempotency_key=key))
    assert created1 is True
    assert created2 is False
    assert first.task_id == second.task_id
    assert len(store.list_tasks()) == 1


def test_explicit_idempotency_key_is_used_verbatim() -> None:
    assert make_idempotency_key("a", explicit="  my-key  ") == "my-key"


def test_auto_key_is_stable_and_case_insensitive() -> None:
    assert make_idempotency_key("Fix   Login", "P") == make_idempotency_key("fix login", "p")


def test_lease_prevents_two_owners(store: Store) -> None:
    task = Task(title="t")
    store.create_task(task)
    assert store.claim_task(task.task_id, "owner-a", lease_seconds=60) is True
    assert store.claim_task(task.task_id, "owner-b", lease_seconds=60) is False
    # Re-claiming by the same owner is fine (idempotent).
    assert store.claim_task(task.task_id, "owner-a", lease_seconds=60) is True


def test_expired_lease_can_be_stolen(store: Store) -> None:
    task = Task(title="t")
    store.create_task(task)
    assert store.claim_task(task.task_id, "owner-a", lease_seconds=-1) is True
    assert store.claim_task(task.task_id, "owner-b", lease_seconds=60) is True


def test_release_allows_another_owner(store: Store) -> None:
    task = Task(title="t")
    store.create_task(task)
    store.claim_task(task.task_id, "owner-a")
    store.release_task(task.task_id, "owner-a")
    assert store.claim_task(task.task_id, "owner-b") is True


def test_events_are_append_only_and_ordered(store: Store) -> None:
    task = Task(title="t")
    store.create_task(task)
    store.set_status(task.task_id, TaskStatus.QUEUED, actor="dispatcher")
    store.set_status(task.task_id, TaskStatus.RUNNING, actor="dispatcher")
    kinds = [e.kind for e in store.list_events(task.task_id)]
    assert kinds == ["status:queued", "status:running"]
