"""Client for the OpenHands agent server and the Cloud app server.

Two backends are supported behind one small interface:

``AgentServerClient``
    Talks to the agent server inside the current sandbox
    (``X-Session-API-Key`` auth). Conversations run in *this* workspace, so the
    dispatcher can start a sub-agent conversation that edits the same repo.

``CloudClient``
    Talks to the OpenHands Cloud app server (Bearer auth) to start
    conversations in their own sandboxes — used when work must be fully
    isolated. Not required for local operation.

Idempotency
-----------
The agent server accepts a **client-supplied** ``conversation_id``. We therefore
derive the conversation id deterministically from the task id
(:func:`conversation_id_for_task`) and always send it. A retried POST after a
timeout can never create a second conversation: either the first request won,
or our POST creates the one conversation with that exact id.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from .logging_setup import get_logger, redact

log = get_logger("ohp.agent_server")

# Deterministic namespace so a task id always maps to the same conversation id.
_CONV_NAMESPACE = uuid.UUID("6f1d4b2a-8c3e-4a9f-9c2b-2f7a1e5d8b40")


def conversation_id_for_task(task_id: str) -> str:
    """Map a task id to a stable conversation UUID (idempotency anchor)."""
    return str(uuid.uuid5(_CONV_NAMESPACE, task_id))


class AgentServerError(RuntimeError):
    """Raised for non-retryable agent-server failures."""


class AgentServerUnavailable(AgentServerError):
    """Raised when the agent server cannot be reached or is timing out."""


@dataclass
class ConversationState:
    """A trimmed view of a conversation, enough to drive the task lifecycle."""

    conversation_id: str
    execution_status: str
    event_count: int = 0
    last_event_kind: str | None = None
    final_response: str | None = None
    activated_skills: list[str] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.execution_status in {
            "finished",
            "error",
            "stuck",
            "cancelled",
        }


class AgentServerClient:
    """Thin, dependency-free HTTP client for the local agent server."""

    def __init__(
        self,
        base_url: str,
        session_api_key: str,
        *,
        timeout: float = 30.0,
        default_model: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._key = session_api_key
        self._timeout = timeout
        self._default_model = default_model
        self._model_cache: str | None = default_model

    def default_model(self) -> str | None:
        """Resolve the model to use when a task says ``inherit``.

        Resolution order: the value passed to the constructor (from
        ``OHP_MODEL``) → the model in the server's ``/api/settings`` → ``None``.
        Model names are environment-specific, so nothing is hard-coded here.
        """
        if self._model_cache:
            return self._model_cache
        try:
            status, payload = self._request("GET", "/api/settings", timeout=10)
        except AgentServerUnavailable:
            return None
        if status != 200 or not isinstance(payload, dict):
            return None
        model = (
            payload.get("agent_settings", {})
            .get("llm", {})
            .get("model")
        )
        if isinstance(model, str) and model:
            self._model_cache = model
            return model
        return None

    # -- low level -------------------------------------------------------- #

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("X-Session-API-Key", self._key)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self._timeout
            ) as response:
                raw = response.read().decode("utf-8")
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:  # server answered with 4xx/5xx
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"detail": raw}
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentServerUnavailable(redact(str(exc))) from exc

    # -- health ----------------------------------------------------------- #

    def server_info(self) -> dict[str, Any]:
        status, payload = self._request("GET", "/server_info", timeout=5)
        if status != 200:
            raise AgentServerError(f"server_info failed: {status} {payload}")
        return payload or {}

    def is_available(self) -> bool:
        try:
            self.server_info()
            return True
        except AgentServerError:
            return False

    # -- conversations ---------------------------------------------------- #

    def create_conversation(
        self,
        *,
        conversation_id: str,
        prompt: str,
        working_dir: str,
        tools: list[str] | None = None,
        model: str | None = None,
        max_iterations: int = 60,
        reasoning_effort: str = "high",
        run: bool = True,
    ) -> str:
        """Create (or resolve) a conversation with a deterministic id.

        Returns the conversation id. Because the id is supplied by us, calling
        this twice after a timeout is safe: the second call either recreates the
        same id or returns 409, both of which are reconciled by the caller.
        """
        body: dict[str, Any] = {
            "workspace": {"kind": "LocalWorkspace", "working_dir": working_dir},
            "conversation_id": conversation_id,
            "max_iterations": max_iterations,
            "confirmation_policy": {"kind": "NeverConfirm"},
            "security_analyzer": {"kind": "LLMSecurityAnalyzer"},
            "initial_message": {
                "content": [{"type": "text", "text": prompt}],
                "run": run,
            },
            "tool_module_qualnames": {
                "terminal": "openhands.tools.terminal.definition",
                "file_editor": "openhands.tools.file_editor.definition",
                "task_tracker": "openhands.tools.task_tracker.definition",
            },
            "autotitle": True,
        }
        if tools:
            body["agent"] = {
                "kind": "Agent",
                "tools": [{"name": name} for name in tools],
            }
        # The agent server requires an explicit model on every conversation.
        # "inherit" is resolved to the server's configured model.
        resolved_model = model or self.default_model()
        if resolved_model:
            llm_block: dict[str, Any] = {"model": resolved_model}
            if reasoning_effort:
                llm_block["reasoning_effort"] = reasoning_effort
            body.setdefault("agent", {})["llm"] = llm_block

        status, payload = self._request("POST", "/api/conversations", body)
        if status in (200, 201):
            return payload.get("id", conversation_id) if payload else conversation_id
        if status == 409:
            # Already exists — idempotent success.
            log.info("conversation already exists", extra={"conversation_id": conversation_id})
            return conversation_id
        raise AgentServerError(f"create_conversation failed: {status} {redact(str(payload))}")

    def get_conversation(self, conversation_id: str) -> ConversationState | None:
        status, payload = self._request(
            "GET", f"/api/conversations?ids={conversation_id}"
        )
        if status == 404 or not payload:
            return None
        items = payload if isinstance(payload, list) else payload.get("items", [])
        if not items:
            return None
        item = items[0]
        if not isinstance(item, dict):
            return None
        return ConversationState(
            conversation_id=item.get("id", conversation_id),
            execution_status=item.get("execution_status", "unknown"),
            activated_skills=item.get("activated_knowledge_skills"),
        )

    def wait_for_completion(
        self,
        conversation_id: str,
        *,
        timeout_s: int,
        poll_interval_s: int = 5,
    ) -> ConversationState:
        """Poll until the conversation reaches a terminal execution status.

        Raises :class:`AgentServerUnavailable` on timeout — the caller treats
        that as ``outcome_unknown`` and reconciles rather than blindly retrying.
        """
        import time

        deadline = time.time() + timeout_s
        state: ConversationState | None = None
        while time.time() < deadline:
            state = self.get_conversation(conversation_id)
            if state is not None and state.is_terminal:
                return state
            time.sleep(poll_interval_s)
        if state is None:
            raise AgentServerUnavailable("conversation never appeared")
        raise AgentServerUnavailable(
            f"timed out after {timeout_s}s (last status={state.execution_status})"
        )

    def final_response(self, conversation_id: str) -> str | None:
        """Return the agent's final message, if any."""
        status, payload = self._request(
            "GET", f"/api/conversations/{conversation_id}/agent_final_response"
        )
        if status != 200 or payload is None:
            return None
        if isinstance(payload, str):
            return payload
        return payload.get("response") or payload.get("text")

    def events(self, conversation_id: str, limit: int = 50) -> list[dict[str, Any]]:
        status, payload = self._request(
            "GET",
            f"/api/conversations/{conversation_id}/events/search"
            f"?limit={limit}&sort_order=TIMESTAMP_DESC",
        )
        if status != 200 or not payload:
            return []
        return payload.get("items", []) if isinstance(payload, dict) else payload

    def interrupt(self, conversation_id: str) -> bool:
        status, _ = self._request(
            "POST", f"/api/conversations/{conversation_id}/interrupt"
        )
        return status in (200, 202, 204)


class CloudClient:
    """Minimal OpenHands Cloud app-server client (Bearer auth).

    Only the delegation path is implemented: start a conversation in its own
    sandbox and poll the start task until it is ready.
    """

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._key}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"detail": raw}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentServerUnavailable(redact(str(exc))) from exc

    def users_me(self) -> dict[str, Any]:
        status, payload = self._request("GET", "/api/v1/users/me")
        if status != 200:
            raise AgentServerError(f"users/me failed: {status}")
        return payload or {}

    def start_conversation(
        self,
        *,
        prompt: str,
        repository: str | None = None,
        branch: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Start a delegated conversation. Returns the raw start-task record."""
        body: dict[str, Any] = {
            "initial_message": {"content": [{"type": "text", "text": prompt}]}
        }
        if repository:
            body["selected_repository"] = repository
        if branch:
            body["selected_branch"] = branch
        if title:
            body["title"] = title
        status, payload = self._request("POST", "/api/v1/app-conversations", body)
        if status not in (200, 201):
            raise AgentServerError(f"start_conversation failed: {status}")
        return payload or {}

    def poll_start_task(self, start_task_id: str) -> dict[str, Any]:
        status, payload = self._request(
            "GET", f"/api/v1/app-conversations/start-tasks?ids={start_task_id}"
        )
        if status != 200:
            raise AgentServerError(f"start-task poll failed: {status}")
        return payload or {}
