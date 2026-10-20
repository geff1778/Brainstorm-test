"""End-to-end dispatcher tests against an in-memory agent server."""

from __future__ import annotations

from ohp.agent_server import AgentServerUnavailable, conversation_id_for_task
from ohp.dispatcher import Dispatcher
from ohp.models import TaskStatus


def test_submit_is_idempotent(dispatcher: Dispatcher) -> None:
    _, created1 = dispatcher.submit("Fix the login bug", project="p")
    _, created2 = dispatcher.submit("Fix the login bug", project="p")
    assert created1 is True
    assert created2 is False
    assert len(dispatcher.store.list_tasks()) == 1


def test_high_risk_task_waits_for_approval(dispatcher: Dispatcher) -> None:
    task, _ = dispatcher.submit("Delete production data", project="p")
    assert task.status is TaskStatus.WAITING_FOR_APPROVAL
    # Running it before approval is a no-op.
    assert dispatcher.run(task.task_id).status is TaskStatus.WAITING_FOR_APPROVAL
    approved = dispatcher.approve(task.task_id)
    assert approved.status is TaskStatus.QUEUED


def test_run_completes_and_parses_result(dispatcher: Dispatcher, fake_agent) -> None:
    task, _ = dispatcher.submit("Add a health endpoint", project="p")
    done = dispatcher.run(task.task_id)
    assert done.status is TaskStatus.COMPLETED
    assert done.result is not None
    assert done.result.summary == "did the thing"
    assert done.attempts == 1
    assert done.conversation_id == conversation_id_for_task(task.task_id)


def test_conversation_id_is_deterministic_and_reused(dispatcher: Dispatcher, fake_agent) -> None:
    task, _ = dispatcher.submit("Add a health endpoint", project="p")
    dispatcher.run(task.task_id)
    # A second run of a completed task is a no-op and does not create a new convo.
    dispatcher.run(task.task_id)
    ids = [c["conversation_id"] for c in fake_agent.created]
    assert ids == [conversation_id_for_task(task.task_id)]


def test_create_timeout_parks_task_as_outcome_unknown(
    dispatcher: Dispatcher, fake_agent
) -> None:
    fake_agent.fail_create = AgentServerUnavailable("timed out")
    task, _ = dispatcher.submit("Do a thing", project="p")
    result = dispatcher.run(task.task_id)
    assert result.status is TaskStatus.OUTCOME_UNKNOWN
    # No duplicate conversation was created.
    assert fake_agent.created == []


def test_reconcile_requeues_when_conversation_never_existed(dispatcher: Dispatcher, fake_agent) -> None:
    fake_agent.fail_create = AgentServerUnavailable("timed out")
    task, _ = dispatcher.submit("Do a thing", project="p")
    dispatcher.run(task.task_id)
    # Conversation was never created -> reconcile safely requeues.
    fake_agent.fail_create = None
    reconciled = dispatcher.run(task.task_id)
    assert reconciled.status is TaskStatus.QUEUED


def test_dependencies_block_until_parent_completes(dispatcher: Dispatcher) -> None:
    parent, _ = dispatcher.submit(
        "Build feature X",
        project="p",
        subtasks=[
            {"title": "step 1", "role": "developer"},
            {"title": "step 2", "role": "qa"},
        ],
    )
    children = dispatcher.store.list_tasks(parent_task_id=parent.task_id)
    assert len(children) == 2
    # step 2 depends on step 1
    second = [c for c in children if c.title == "step 2"][0]
    assert len(second.dependencies) == 1


def test_failure_is_retried_then_failed(dispatcher: Dispatcher, fake_agent) -> None:
    fake_agent.final_text = '```json\n{"status":"failed","summary":"connection reset"}\n```'
    task, _ = dispatcher.submit("Do flaky thing", project="p")
    done = dispatcher.run(task.task_id)
    # retryable -> requeued (status queued) after backoff
    assert done.status is TaskStatus.QUEUED


def test_fatal_failure_is_not_retried(dispatcher: Dispatcher, fake_agent) -> None:
    fake_agent.final_text = '```json\n{"status":"failed","summary":"403 Forbidden"}\n```'
    task, _ = dispatcher.submit("Do forbidden thing", project="p")
    done = dispatcher.run(task.task_id)
    assert done.status is TaskStatus.FAILED


def test_status_reports_counts(dispatcher: Dispatcher) -> None:
    dispatcher.submit("Fix a bug", project="p")
    report = dispatcher.status()
    assert "counts" in report
    assert report["counts"].get("queued") == 1
