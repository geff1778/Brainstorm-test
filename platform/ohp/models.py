"""Domain models and the task/result contracts.

These dataclasses are the *single source of truth* for what a task is. They are
deliberately storage-agnostic: :mod:`ohp.store` persists them, but nothing here
knows about SQLite. Keeping the contract in one place means the dispatcher, the
CLI and the tests all speak the same language.

The two central contracts are:

``Task``
    A unit of work with a stable ``task_id`` and an ``idempotency_key`` that
    guarantees the same logical request never creates two tasks.

``TaskResult``
    What an executor must return. ``to_dict`` matches the JSON we ask sub-agents
    to emit, so a result can round-trip through the database unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class TaskStatus(str, Enum):
    """Lifecycle states. See :mod:`ohp.state_machine` for legal transitions."""

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_AGENT = "waiting_for_agent"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RETRY_WAIT = "retry_wait"
    BLOCKED = "blocked"
    OUTCOME_UNKNOWN = "outcome_unknown"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """A terminal state has no outgoing transitions in normal operation."""
        return self in _TERMINAL


_TERMINAL = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class TaskType(str, Enum):
    """Classification of a user request. Drives executor routing."""

    QUESTION = "question"
    RESEARCH = "research"
    CODING = "coding"
    DEBUGGING = "debugging"
    ARCHITECTURE = "architecture"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    DEVOPS = "devops"
    MAINTENANCE = "maintenance"
    RELEASE = "release"
    SECURITY = "security"
    MULTI_PROJECT = "multi_project"


class Priority(str, Enum):
    """Ordered priority. ``P0`` is most urgent; sorting is by ``rank``."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"

    @property
    def rank(self) -> int:
        return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}[self.value]


class Complexity(str, Enum):
    TRIVIAL = "trivial"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def utcnow() -> str:
    """Return an ISO-8601 UTC timestamp with a trailing ``Z``."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_task_id() -> str:
    """Generate a task id with a sortable ``task_`` prefix."""
    return f"task_{uuid.uuid4().hex}"


_WS = re.compile(r"\s+")


def make_idempotency_key(
    *parts: str, explicit: str | None = None, **kwargs: Any
) -> str:
    """Build a deterministic key for a logical operation.

    If ``explicit`` is given it is used verbatim — this is how a caller (or an
    automation) supplies its own key so a retried delivery maps to the same
    task. Otherwise the key is the SHA-256 of the normalised parts plus sorted
    keyword arguments, so identical inputs always yield the same key.
    """
    if explicit:
        return explicit.strip()
    payload = {
        "parts": [_WS.sub(" ", str(p)).strip().lower() for p in parts if p is not None],
        "kwargs": {k: kwargs[k] for k in sorted(kwargs)},
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return "auto:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


@dataclass
class TaskResult:
    """The single result contract every executor returns.

    ``to_dict`` produces the JSON object we instruct sub-agents to emit, and
    :meth:`from_dict` parses it back, tolerating missing fields.
    """

    status: str = "completed"
    summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    tests_run: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    artifacts: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    next_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TaskResult:
        if not data:
            return cls(status="failed", summary="empty result")
        known = {f: data.get(f) for f in cls.__dataclass_fields__ if f in data}
        # Coerce list fields defensively: a model may return a string.
        for list_field in (
            "changed_files",
            "tests_run",
            "evidence",
            "artifacts",
            "blockers",
        ):
            value = known.get(list_field)
            if isinstance(value, str):
                known[list_field] = [value] if value else []
            elif value is None:
                known.pop(list_field, None)
        return cls(**known)


@dataclass
class Task:
    """A unit of work tracked end-to-end by the platform."""

    title: str
    description: str = ""
    task_type: TaskType = TaskType.CODING
    priority: Priority = Priority.P2
    complexity: Complexity = Complexity.SMALL
    risk: RiskLevel = RiskLevel.LOW
    project: str = "default"
    role: str = "developer"
    working_dir: str | None = None
    acceptance: list[str] = field(default_factory=list)
    context: str = ""
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    requires_approval: bool = False
    model: str = "inherit"
    max_iterations: int = 60
    timeout_s: int = 1800
    max_attempts: int = 3

    task_id: str = field(default_factory=new_task_id)
    parent_task_id: str | None = None
    idempotency_key: str = ""
    status: TaskStatus = TaskStatus.CREATED
    attempts: int = 0
    conversation_id: str | None = None
    result: TaskResult | None = None
    error: str | None = None
    blocked_reason: str | None = None
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            self.idempotency_key = make_idempotency_key(
                self.title, self.project, self.task_type.value
            )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly view (real lists) for API/CLI output."""
        row = asdict(self)
        row["task_type"] = self.task_type.value
        row["priority"] = self.priority.value
        row["complexity"] = self.complexity.value
        row["risk"] = self.risk.value
        row["status"] = self.status.value
        row["result"] = self.result.to_dict() if self.result else None
        return row

    def to_row(self) -> dict[str, Any]:
        """Storage view: lists serialised to JSON text for SQLite binding."""
        row = self.to_dict()
        for list_field in ("acceptance", "tools", "skills", "dependencies"):
            row[list_field] = json.dumps(row[list_field], ensure_ascii=False)
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Task:
        """Rebuild a :class:`Task` from a stored row (tolerant of JSON text)."""
        data = dict(row)
        data["task_type"] = TaskType(data.get("task_type", "coding"))
        data["priority"] = Priority(data.get("priority", "P2"))
        data["complexity"] = Complexity(data.get("complexity", "small"))
        data["risk"] = RiskLevel(data.get("risk", "low"))
        data["status"] = TaskStatus(data.get("status", "created"))
        result = data.get("result")
        data["result"] = TaskResult.from_dict(result) if result else None
        for list_field in ("acceptance", "tools", "skills", "dependencies"):
            value = data.get(list_field)
            if isinstance(value, str):
                try:
                    data[list_field] = json.loads(value)
                except json.JSONDecodeError:
                    data[list_field] = []
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class Event:
    """An append-only audit record. Never contains secrets."""

    task_id: str
    kind: str
    actor: str = "dispatcher"
    detail: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: str = field(default_factory=utcnow)


@dataclass
class Project:
    """A registered project the dispatcher can route work to."""

    name: str
    repo: str = ""
    working_dir: str = ""
    default_branch: str = "main"
    stack: list[str] = field(default_factory=list)
    build_cmd: str = ""
    test_cmd: str = ""
    skills: list[str] = field(default_factory=list)
    mcp: list[str] = field(default_factory=list)
    status: str = "active"
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
