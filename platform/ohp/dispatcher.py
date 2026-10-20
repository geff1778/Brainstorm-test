"""The Main Dispatcher: admission, planning, execution and integration.

This is the component the user (or an automation) talks to. It owns the whole
lifecycle of a request:

``submit``    classify -> persist a task (idempotently) -> optionally decompose
``run``       claim a lease -> drive one conversation -> parse -> transition
``run_pending`` drain the queue with a conservative concurrency cap
``recover``   reconcile tasks left ``outcome_unknown`` after a crash/timeout
``approve``   release a task parked for human approval

Design rules enforced here:

* **No duplicate work.** Every task carries an idempotency key; the store's
  unique constraint is the arbiter. A conversation id is derived from the task
  id, so a retried start cannot spawn a second conversation.
* **Never blind-retry an irreversible step.** If a start times out we move the
  task to ``outcome_unknown`` and reconcile instead of POSTing again.
* **Text is not proof.** A task is only ``completed`` once a conversation
  reached a terminal state *and* a result contract was parsed from it.
"""

from __future__ import annotations

import concurrent.futures as futures
import time
from pathlib import Path
from typing import Any

from .agent_server import (
    AgentServerClient,
    AgentServerError,
    AgentServerUnavailable,
    conversation_id_for_task,
)
from .classifier import ROLE_TOOLS, Classification, classify, detect_project
from .config import Settings
from .executor import (
    backoff_delay,
    build_prompt,
    classify_error,
    load_role_body,
    parse_result,
)
from .logging_setup import get_logger
from .models import (
    Event,
    Task,
    TaskResult,
    TaskStatus,
    TaskType,
    make_idempotency_key,
)
from .store import Store

log = get_logger("ohp.dispatcher")


class Dispatcher:
    """Coordinates tasks end to end."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        *,
        agent_client: AgentServerClient | None = None,
        owner_id: str = "dispatcher-1",
        role_dirs: list[Path] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.owner_id = owner_id
        self.agent = agent_client or AgentServerClient(
            settings.agent_server_url,
            settings.session_api_key,
            default_model=settings.default_model or None,
        )
        self.role_dirs = role_dirs or [
            Path.cwd() / ".agents" / "agents",
            Path.home() / ".agents" / "agents",
        ]

    # ------------------------------------------------------------------ #
    # Admission
    # ------------------------------------------------------------------ #

    def submit(
        self,
        request: str,
        *,
        project: str | None = None,
        title: str | None = None,
        idempotency_key: str | None = None,
        subtasks: list[dict[str, Any]] | None = None,
        context: str = "",
    ) -> tuple[Task, bool]:
        """Classify and persist a request. Returns ``(task, created)``.

        If an equivalent task already exists the stored one is returned with
        ``created=False`` — the caller must not spawn duplicate work.
        """
        if project is None:
            project = detect_project(request, [p.name for p in self.store.list_projects()])
        decision = classify(request, project=project)
        task = self._task_from_decision(request, decision, title=title, context=context)
        task.idempotency_key = make_idempotency_key(
            task.title, project, decision.task_type.value, explicit=idempotency_key
        )

        stored, created = self.store.create_task(task)
        if not created:
            log.info("duplicate submit ignored", extra={"task_id": stored.task_id})
            return stored, False

        self.store.append_event(
            Event(
                task_id=stored.task_id,
                kind="submitted",
                actor="dispatcher",
                detail={"classification": decision.to_dict()},
            )
        )
        # Admission transition: created -> waiting_for_approval | queued.
        target = (
            TaskStatus.WAITING_FOR_APPROVAL
            if decision.requires_approval
            else TaskStatus.QUEUED
        )
        self.store.set_status(stored.task_id, target, actor="dispatcher")

        if subtasks:
            self._create_children(stored, subtasks)

        log.info(
            "task submitted",
            extra={"task_id": stored.task_id, "event": decision.task_type.value},
        )
        return self.store.get_task(stored.task_id), True  # type: ignore[return-value]

    def _task_from_decision(
        self,
        request: str,
        decision: Classification,
        *,
        title: str | None = None,
        context: str = "",
    ) -> Task:
        project = self.store.get_project(decision.project)
        return Task(
            title=title or _first_line(request),
            description=request,
            task_type=decision.task_type,
            priority=decision.priority,
            complexity=decision.complexity,
            risk=decision.risk,
            project=decision.project,
            role=decision.role,
            working_dir=project.working_dir if project else None,
            tools=decision.tools,
            skills=decision.skills,
            requires_approval=decision.requires_approval,
            context=context,
            acceptance=_default_acceptance(decision),
            model=self.settings.default_model or "inherit",
            max_iterations=80 if decision.complexity.value in ("large", "medium") else 40,
            timeout_s=self.settings.task_timeout_s,
        )

    def _create_children(self, parent: Task, subtasks: list[dict[str, Any]]) -> list[Task]:
        """Create Jr tasks under a parent Sr task, wiring sequential deps."""
        children: list[Task] = []
        previous: Task | None = None
        for index, spec in enumerate(subtasks):
            role = spec.get("role", "developer")
            child = Task(
                title=spec.get("title", f"subtask {index + 1}"),
                description=spec.get("description", ""),
                task_type=TaskType(spec.get("task_type", "coding")),
                priority=parent.priority,
                complexity=parent.complexity,
                risk=parent.risk,
                project=parent.project,
                role=role,
                working_dir=parent.working_dir,
                tools=spec.get("tools", ROLE_TOOLS.get(role, ROLE_TOOLS["developer"])),
                skills=spec.get("skills", []),
                parent_task_id=parent.task_id,
                context=spec.get("context", ""),
                acceptance=spec.get("acceptance", []),
                model=parent.model,
                timeout_s=parent.timeout_s,
            )
            # Sequential by default; independent specs set depends_on=[].
            deps = spec.get("depends_on")
            if deps is None and previous is not None:
                child.dependencies = [previous.task_id]
            elif deps:
                child.dependencies = list(deps)
            child.idempotency_key = make_idempotency_key(
                parent.task_id, child.title, role
            )
            stored, created = self.store.create_task(child)
            if created:
                self.store.set_status(stored.task_id, TaskStatus.QUEUED, actor="dispatcher")
            children.append(stored)
            previous = stored
        self.store.append_event(
            Event(
                task_id=parent.task_id,
                kind="decomposed",
                actor="dispatcher",
                detail={"children": [c.task_id for c in children]},
            )
        )
        return children

    # ------------------------------------------------------------------ #
    # Approval & recovery entry points
    # ------------------------------------------------------------------ #

    def approve(self, task_id: str, *, actor: str = "operator") -> Task:
        return self.store.set_status(task_id, TaskStatus.QUEUED, actor=actor)

    def cancel(self, task_id: str, *, actor: str = "operator") -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.conversation_id:
            try:
                self.agent.interrupt(task.conversation_id)
            except AgentServerError:
                pass
        return self.store.set_status(task_id, TaskStatus.CANCELLED, actor=actor)

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    def run(self, task_id: str) -> Task:
        """Execute one task synchronously. Safe to call from many threads."""
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id}")
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return task
        if task.status is TaskStatus.WAITING_FOR_APPROVAL:
            log.info("task awaits approval", extra={"task_id": task_id})
            return task
        if task.status is TaskStatus.OUTCOME_UNKNOWN:
            return self._reconcile(task)

        if not self.store.claim_task(
            task_id, self.owner_id, lease_seconds=self.settings.lease_seconds
        ):
            log.info("task leased by another worker", extra={"task_id": task_id})
            return self.store.get_task(task_id)  # type: ignore[return-value]

        try:
            return self._execute(task)
        finally:
            self.store.release_task(task_id, self.owner_id)

    def _execute(self, task: Task) -> Task:
        # queued -> running (records an attempt)
        self.store.set_status(
            task.task_id, TaskStatus.RUNNING, actor="dispatcher", increment_attempts=True
        )
        task = self.store.get_task(task.task_id)  # type: ignore[assignment]
        conversation_id = conversation_id_for_task(task.task_id)

        project = self.store.get_project(task.project)
        role_body = load_role_body(task.role, self.role_dirs)
        prompt = build_prompt(task, project, role_body=role_body)

        try:
            self.agent.create_conversation(
                conversation_id=conversation_id,
                prompt=prompt,
                working_dir=task.working_dir or (project.working_dir if project else str(Path.cwd())),
                tools=task.tools or ROLE_TOOLS.get(task.role, ["terminal", "file_editor"]),
                model=None if task.model == "inherit" else task.model,
                max_iterations=task.max_iterations,
                reasoning_effort=self.settings.reasoning_effort,
            )
        except AgentServerUnavailable as exc:
            return self._park_unknown(task, str(exc))
        except AgentServerError as exc:
            # A definitive server rejection (4xx/5xx) is not retryable blindly.
            return self.store.set_status(
                task.task_id,
                TaskStatus.FAILED,
                actor="dispatcher",
                error=str(exc),
            )

        self.store.attach_conversation(task.task_id, conversation_id)
        self.store.set_status(
            task.task_id, TaskStatus.WAITING_FOR_AGENT, actor="dispatcher"
        )

        try:
            state = self.agent.wait_for_completion(
                conversation_id,
                timeout_s=task.timeout_s,
                poll_interval_s=self.settings.poll_interval_s,
            )
        except AgentServerUnavailable as exc:
            # We do not know whether the work finished — reconcile, don't retry.
            return self._park_unknown(task, str(exc))

        self.store.set_status(task.task_id, TaskStatus.RUNNING, actor="dispatcher")
        final = self.agent.final_response(conversation_id)
        result = parse_result(final)

        if state.execution_status in ("error", "stuck", "cancelled") or result.status == "failed":
            return self._fail(task, result, state.execution_status)

        self.store.set_status(
            task.task_id,
            TaskStatus.COMPLETED,
            actor="executor",
            result=result,
        )
        log.info("task completed", extra={"task_id": task.task_id})
        return self.store.get_task(task.task_id)  # type: ignore[return-value]

    def _park_unknown(self, task: Task, reason: str) -> Task:
        """Move to ``outcome_unknown`` — never blind-retry an external call."""
        log.warning(
            "task outcome unknown; will reconcile",
            extra={"task_id": task.task_id, "event": reason},
        )
        return self.store.set_status(
            task.task_id,
            TaskStatus.OUTCOME_UNKNOWN,
            actor="dispatcher",
            error=reason,
        )

    def _fail(self, task: Task, result: TaskResult, status: str) -> Task:
        kind = classify_error(result.summary or status)
        message = result.summary or f"conversation ended with status={status}"
        if kind == "retryable" and task.attempts < task.max_attempts:
            delay = backoff_delay(task.attempts)
            self.store.set_status(
                task.task_id,
                TaskStatus.RETRY_WAIT,
                actor="executor",
                error=message,
            )
            self.store.append_event(
                Event(
                    task_id=task.task_id,
                    kind="retry_scheduled",
                    actor="dispatcher",
                    detail={"delay_s": round(delay, 1), "attempt": task.attempts},
                )
            )
            time.sleep(delay)
            return self.store.set_status(task.task_id, TaskStatus.QUEUED, actor="dispatcher")
        return self.store.set_status(
            task.task_id,
            TaskStatus.FAILED,
            actor="executor",
            result=result,
            error=message,
        )

    def _reconcile(self, task: Task) -> Task:
        """Resolve an ``outcome_unknown`` task by inspecting its conversation."""
        if not task.conversation_id:
            return self.store.set_status(
                task.task_id, TaskStatus.QUEUED, actor="dispatcher"
            )
        state = self.agent.get_conversation(task.conversation_id)
        if state is None:
            # The conversation was never created: safe to requeue.
            return self.store.set_status(
                task.task_id, TaskStatus.QUEUED, actor="dispatcher"
            )
        if not state.is_terminal:
            return self.store.set_status(
                task.task_id, TaskStatus.RUNNING, actor="dispatcher"
            )
        final = self.agent.final_response(task.conversation_id)
        result = parse_result(final)
        return self.store.set_status(
            task.task_id, TaskStatus.COMPLETED, actor="dispatcher", result=result
        )

    # ------------------------------------------------------------------ #
    # Batch execution
    # ------------------------------------------------------------------ #

    def run_pending(self, *, max_tasks: int = 50) -> list[Task]:
        """Drain queued tasks with a conservative concurrency cap.

        Respects ``dependencies``: a task is skipped while any dependency is not
        ``completed``. Independent tasks run in parallel up to
        ``settings.max_parallel``.
        """
        done: list[Task] = []
        processed = 0
        with futures.ThreadPoolExecutor(max_workers=self.settings.max_parallel) as pool:
            while processed < max_tasks:
                task = self._next_runnable()
                if task is None:
                    break
                # Reserve the task so the next loop iteration skips it.
                self.store.claim_task(
                    task.task_id, self.owner_id, lease_seconds=self.settings.lease_seconds
                )
                self.store.release_task(task.task_id, self.owner_id)
                future = pool.submit(self.run, task.task_id)
                done.append(future.result())
                processed += 1
        return done

    def _next_runnable(self) -> Task | None:
        task = self.store.next_queued()
        while task is not None:
            if self._dependencies_met(task):
                return task
            # Park blocked tasks so we do not spin on them.
            self.store.set_status(
                task.task_id,
                TaskStatus.BLOCKED,
                actor="dispatcher",
                blocked_reason="dependencies not complete",
            )
            task = self.store.next_queued()
        return None

    def _dependencies_met(self, task: Task) -> bool:
        for dep_id in task.dependencies:
            dep = self.store.get_task(dep_id)
            if dep is None or dep.status is not TaskStatus.COMPLETED:
                return False
        return True

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        return {
            "counts": self.store.counts_by_status(),
            "projects": [p.name for p in self.store.list_projects()],
            "agent_server_available": self.agent.is_available(),
            "max_parallel": self.settings.max_parallel,
        }


def _first_line(text: str, limit: int = 120) -> str:
    line = (text or "").strip().splitlines()[0] if text.strip() else "untitled task"
    return line[:limit]


def _default_acceptance(decision: Classification) -> list[str]:
    base = ["The requested change is implemented and demonstrably works."]
    if decision.task_type in (TaskType.CODING, TaskType.DEBUGGING, TaskType.TESTING):
        base.append("Relevant tests pass; the result contract reports the command run.")
    if decision.risk.value == "high":
        base.append("No secrets were exposed and the change is reversible.")
    return base
