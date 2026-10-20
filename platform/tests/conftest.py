"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from ohp.config import Settings
from ohp.dispatcher import Dispatcher
from ohp.store import Store


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.db_path = str(tmp_path / "platform.db")
    return s


@pytest.fixture()
def store(settings: Settings) -> Store:
    st = Store(settings.db_path)
    yield st
    st.close()


class FakeAgentServer:
    """In-memory stand-in for the agent server.

    It records the conversations it is asked to create so tests can assert on
    idempotency and on the prompt that was actually sent — without any network.
    """

    def __init__(self, *, final_text: str | None = None, status: str = "finished") -> None:
        self.created: list[dict] = []
        self.final_text = final_text or (
            "done\n```json\n"
            '{"status":"completed","summary":"did the thing",'
            '"changed_files":["a.py"],"tests_run":["pytest -q -> 3 passed"],'
            '"evidence":["3 passed"],"blockers":[],"next_action":null}\n```'
        )
        self.status = status
        self._exists: set[str] = set()
        self.fail_create: Exception | None = None

    def create_conversation(self, **kwargs):  # noqa: ANN003
        if self.fail_create:
            raise self.fail_create
        cid = kwargs["conversation_id"]
        self.created.append(kwargs)
        self._exists.add(cid)
        return cid

    def get_conversation(self, conversation_id: str):
        from ohp.agent_server import ConversationState

        if conversation_id not in self._exists:
            return None
        return ConversationState(conversation_id, self.status)

    def wait_for_completion(self, conversation_id: str, **kwargs):  # noqa: ANN003
        return self.get_conversation(conversation_id)

    def final_response(self, conversation_id: str):
        return self.final_text

    def interrupt(self, conversation_id: str) -> bool:
        return True

    def is_available(self) -> bool:
        return True

    def server_info(self):
        return {"version": "fake", "usable_tools": []}


@pytest.fixture()
def fake_agent() -> FakeAgentServer:
    return FakeAgentServer()


@pytest.fixture()
def dispatcher(store: Store, settings: Settings, fake_agent: FakeAgentServer) -> Dispatcher:
    return Dispatcher(
        store,
        settings,
        agent_client=fake_agent,  # type: ignore[arg-type]
        role_dirs=[Path("/nonexistent")],  # no role bodies -> pure prompt
    )
