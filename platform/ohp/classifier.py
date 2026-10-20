"""Deterministic request classifier and router.

The classifier turns a free-text request into a routing decision: task type,
complexity, priority, risk, the executor role, the tools/skills to grant and
whether human approval is required. It is intentionally rule-based — it is
fast, explainable, testable and has no external dependency. An LLM-based
classifier can be layered on later without changing this contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Complexity, Priority, RiskLevel, TaskType

# Keyword -> task type. Order matters: the first match wins, so more specific
# categories are listed before broader ones.
_TYPE_KEYWORDS: tuple[tuple[TaskType, tuple[str, ...]], ...] = (
    (TaskType.SECURITY, ("security", "vulnerab", "cve", "secret leak", "threat", "exploit")),
    (TaskType.RELEASE, ("release", "publish version", "tag v", "cut a version", "changelog")),
    (TaskType.DEVOPS, ("docker", "deploy", "ci/cd", "kubernetes", "render", "pipeline", "infra")),
    (TaskType.TESTING, ("test", "pytest", "unit test", "coverage", "qa", "regression")),
    (TaskType.DOCUMENTATION, ("readme", "document", "docs", "jsdoc", "docstring", "guide")),
    (TaskType.DEBUGGING, ("bug", "fix", "broken", "error", "crash", "traceback", "fails", "debug")),
    (TaskType.ARCHITECTURE, ("architecture", "design", "refactor", "restructure", "schema", "migrat")),
    (TaskType.RESEARCH, ("research", "investigate", "compare", "evaluate", "find out", "explore")),
    (TaskType.QUESTION, ("what is", "how do", "explain", "why does", "question")),
    (TaskType.CODING, ("implement", "add", "create", "build", "feature", "write code", "endpoint")),
)

_RISK_KEYWORDS = (
    "production",
    "delete",
    "drop table",
    "rm -rf",
    "secret",
    "credential",
    "token",
    "migration",
    "payment",
    "auth",
    "deploy",
)

_COMPLEXITY_SIGNALS = {
    Complexity.LARGE: ("system", "platform", "multiple", "end-to-end", "architecture", "migrate"),
    Complexity.MEDIUM: ("module", "component", "api", "service", "refactor", "integrate"),
}


@dataclass
class Classification:
    """The routing decision for one request."""

    task_type: TaskType
    complexity: Complexity
    priority: Priority
    risk: RiskLevel
    role: str
    requires_approval: bool
    project: str
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_type": self.task_type.value,
            "complexity": self.complexity.value,
            "priority": self.priority.value,
            "risk": self.risk.value,
            "role": self.role,
            "requires_approval": self.requires_approval,
            "project": self.project,
            "tools": self.tools,
            "skills": self.skills,
            "rationale": self.rationale,
        }


# Task type -> default executor role.
_ROLE_FOR_TYPE: dict[TaskType, str] = {
    TaskType.QUESTION: "researcher",
    TaskType.RESEARCH: "researcher",
    TaskType.CODING: "developer",
    TaskType.DEBUGGING: "developer",
    TaskType.ARCHITECTURE: "architect",
    TaskType.TESTING: "qa",
    TaskType.DOCUMENTATION: "documenter",
    TaskType.DEVOPS: "devops",
    TaskType.MAINTENANCE: "developer",
    TaskType.RELEASE: "devops",
    TaskType.SECURITY: "security",
    TaskType.MULTI_PROJECT: "architect",
}

# Role -> tools granted (least privilege) and skills attached.
ROLE_TOOLS: dict[str, list[str]] = {
    "researcher": ["file_editor"],
    "developer": ["terminal", "file_editor", "task_tracker"],
    "architect": ["file_editor", "terminal"],
    "qa": ["terminal", "file_editor", "browser_tool_set"],
    "documenter": ["file_editor"],
    "devops": ["terminal", "file_editor"],
    "security": ["terminal", "file_editor"],
    "integrator": ["terminal", "file_editor"],
}

ROLE_SKILLS: dict[str, list[str]] = {
    "researcher": ["research", "technical-writing"],
    "developer": ["python", "testing", "code-review"],
    "architect": ["architecture", "technical-writing"],
    "qa": ["testing", "qa-changes"],
    "documenter": ["technical-writing", "plain-english-content"],
    "devops": ["docker", "github-actions"],
    "security": ["security", "code-review"],
    "integrator": ["github", "code-review"],
}


def _contains(text: str, needles: tuple[str, ...]) -> str | None:
    for needle in needles:
        if needle in text:
            return needle
    return None


def classify(request: str, *, project: str = "default") -> Classification:
    """Classify ``request`` into a routing decision.

    The rules are deliberately simple and observable; ``rationale`` records why
    each decision was made so a reviewer can audit the routing.
    """
    text = (request or "").lower()
    rationale: list[str] = []

    # --- task type ---
    task_type = TaskType.CODING
    for candidate, keywords in _TYPE_KEYWORDS:
        hit = _contains(text, keywords)
        if hit:
            task_type = candidate
            rationale.append(f"type={candidate.value} (matched '{hit}')")
            break
    else:
        rationale.append("type=coding (default)")

    # --- risk ---
    risk_hit = _contains(text, _RISK_KEYWORDS)
    if risk_hit in ("production", "delete", "drop table", "rm -rf", "migration", "payment"):
        risk = RiskLevel.HIGH
    elif risk_hit:
        risk = RiskLevel.MEDIUM
    else:
        risk = RiskLevel.LOW
    if risk_hit:
        rationale.append(f"risk={risk.value} (matched '{risk_hit}')")

    # --- complexity ---
    complexity = Complexity.SMALL
    for level, signals in _COMPLEXITY_SIGNALS.items():
        if _contains(text, signals):
            complexity = level
            break
    if len(text) > 600 and complexity is Complexity.SMALL:
        complexity = Complexity.MEDIUM
        rationale.append("complexity=medium (long request)")
    rationale.append(f"complexity={complexity.value}")

    # --- priority ---
    if risk is RiskLevel.HIGH:
        priority = Priority.P1
    elif task_type in (TaskType.SECURITY, TaskType.RELEASE):
        priority = Priority.P1
    elif task_type in (TaskType.QUESTION, TaskType.DOCUMENTATION):
        priority = Priority.P3
    else:
        priority = Priority.P2
    rationale.append(f"priority={priority.value}")

    role = _ROLE_FOR_TYPE.get(task_type, "developer")
    # A high-risk task is always reviewed by the architect role first.
    if risk is RiskLevel.HIGH and role == "developer":
        role = "architect"
        rationale.append("role upgraded to architect (high risk)")

    requires_approval = risk is RiskLevel.HIGH
    if requires_approval:
        rationale.append("approval required (high risk)")

    return Classification(
        task_type=task_type,
        complexity=complexity,
        priority=priority,
        risk=risk,
        role=role,
        requires_approval=requires_approval,
        project=project,
        tools=list(ROLE_TOOLS.get(role, ROLE_TOOLS["developer"])),
        skills=list(ROLE_SKILLS.get(role, [])),
        rationale=rationale,
    )


def detect_project(request: str, known_projects: list[str]) -> str:
    """Best-effort project detection from a request.

    A project is selected when its name (or a token of it) appears in the
    request; otherwise ``default`` is used.
    """
    text = (request or "").lower()
    for name in known_projects:
        tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) > 3]
        if name.lower() in text or any(t in text for t in tokens):
            return name
    return "default"
