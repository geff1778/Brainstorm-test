"""Task prompt construction, result parsing and error classification.

The executor layer is the boundary between the platform's typed ``Task`` and a
sub-agent conversation. Two responsibilities live here:

* :func:`build_prompt` renders a **self-contained** prompt. A delegated
  conversation has no memory of ours, so everything it needs — goal, project,
  constraints, acceptance criteria and the exact output contract — is embedded.
* :func:`parse_result` extracts the JSON result contract from the agent's final
  message, tolerating prose around the fence.

:func:`classify_error` decides whether a failure is worth retrying. This is the
difference between a resilient system and one that hammers a broken endpoint.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Project, Task, TaskResult

# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #

RESULT_CONTRACT = """\
When you are done, end your final message with a single fenced JSON object and
nothing after it, using exactly these keys:

```json
{
  "status": "completed | failed | blocked",
  "summary": "one or two sentences on what you did",
  "changed_files": ["path/to/file"],
  "tests_run": ["pytest tests/test_x.py -q  ->  12 passed"],
  "evidence": ["command or fact that proves the change works"],
  "commit_sha": "abc1234 or null",
  "artifacts": ["path/to/artifact"],
  "blockers": ["anything that stopped you"],
  "next_action": "what a human should do next, or null"
}
```
"""


def load_role_body(role: str, search_dirs: list[Path]) -> str | None:
    """Return the Markdown body of the file-based agent for ``role``.

    File-based agents live in ``.agents/agents/<role>.md``; their body is the
    sub-agent's system prompt. We reuse it as role instructions so the platform
    and the SDK agree on one source of truth.
    """
    for base in search_dirs:
        candidate = base / f"{role}.md"
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            # Strip YAML frontmatter if present.
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) == 3:
                    return parts[2].strip()
            return text.strip()
    return None


def build_prompt(
    task: Task,
    project: Project | None = None,
    *,
    role_body: str | None = None,
) -> str:
    """Render a self-contained prompt for a delegated conversation."""
    lines: list[str] = []
    if role_body:
        lines.append(role_body)
        lines.append("")

    lines.append("# Task")
    lines.append(f"- task_id: {task.task_id}")
    lines.append(f"- project: {task.project}")
    if project and project.repo:
        lines.append(f"- repository: {project.repo}")
        lines.append(f"- default branch: {project.default_branch}")
    if task.working_dir or (project and project.working_dir):
        lines.append(f"- working directory: {task.working_dir or project.working_dir}")
    lines.append(f"- role: {task.role}")
    lines.append(f"- type: {task.task_type.value}")
    lines.append("")
    lines.append("## Goal")
    lines.append(task.title)
    if task.description:
        lines.append("")
        lines.append(task.description)
    if task.context:
        lines.append("")
        lines.append("## Context")
        lines.append(task.context)
    if task.acceptance:
        lines.append("")
        lines.append("## Acceptance criteria")
        lines.extend(f"- {item}" for item in task.acceptance)
    if project and project.test_cmd:
        lines.append("")
        lines.append("## Verification")
        lines.append(f"Run `{project.test_cmd}` and report the result.")
    if task.dependencies:
        lines.append("")
        lines.append("## Dependencies")
        lines.append("These tasks must already be complete: " + ", ".join(task.dependencies))

    lines.append("")
    lines.append("## Rules")
    lines.append("- Work only inside the working directory above.")
    lines.append("- Make the smallest change that satisfies the goal.")
    lines.append("- Never print, commit or log secrets or tokens.")
    lines.append("- Do not create pull requests unless explicitly asked.")
    lines.append("")
    lines.append("## Output format")
    lines.append(RESULT_CONTRACT)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Result parsing
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_result(text: str | None) -> TaskResult:
    """Extract the result contract from an agent's final message.

    Looks for the last fenced JSON object first; falls back to the last bare
    ``{...}`` block. If nothing parseable is found the whole message becomes the
    summary and the status is ``completed`` (the conversation did finish) with a
    note — we never silently discard the agent's words.
    """
    if not text or not text.strip():
        return TaskResult(status="completed", summary="agent produced no final message")

    candidates: list[str] = []
    candidates.extend(_FENCE_RE.findall(text))
    # Bare JSON object fallback.
    start = text.rfind("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for raw in reversed(candidates):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and ("status" in data or "summary" in data):
            return TaskResult.from_dict(data)

    return TaskResult(status="completed", summary=text.strip()[:4000])


# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #

RETRYABLE_MARKERS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "rate limit",
    "429",
    "502",
    "503",
    "504",
    "econnreset",
    "unavailable",
)
FATAL_MARKERS = (
    "unauthorized",
    "forbidden",
    "401",
    "403",
    "not found",
    "404",
    "invalid api key",
    "no such file",
)


def classify_error(message: str) -> str:
    """Return ``"retryable"``, ``"fatal"`` or ``"unknown"`` for an error."""
    text = (message or "").lower()
    if any(marker in text for marker in FATAL_MARKERS):
        return "fatal"
    if any(marker in text for marker in RETRYABLE_MARKERS):
        return "retryable"
    return "unknown"


def backoff_delay(attempt: int, *, base: float = 2.0, cap: float = 60.0) -> float:
    """Exponential backoff with full jitter, capped at ``cap`` seconds."""
    import random

    ceiling = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(0, ceiling)
