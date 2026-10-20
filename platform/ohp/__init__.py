"""OpenHands orchestration platform — public API.

Import the pieces you need::

    from ohp import Dispatcher, Settings, Store

    store = Store(Settings().db_path)
    dispatcher = Dispatcher(store, Settings())
    task, created = dispatcher.submit("Fix the login bug")
    dispatcher.run(task.task_id)
"""

from .agent_server import AgentServerClient, conversation_id_for_task
from .classifier import Classification, classify, detect_project
from .config import Settings
from .dispatcher import Dispatcher
from .models import (
    Complexity,
    Event,
    Priority,
    Project,
    RiskLevel,
    Task,
    TaskResult,
    TaskStatus,
    TaskType,
    make_idempotency_key,
)
from .store import Store

__version__ = "1.0.0"

__all__ = [
    "AgentServerClient",
    "Classification",
    "Complexity",
    "Dispatcher",
    "Event",
    "Priority",
    "Project",
    "RiskLevel",
    "Settings",
    "Store",
    "Task",
    "TaskResult",
    "TaskStatus",
    "TaskType",
    "classify",
    "conversation_id_for_task",
    "detect_project",
    "make_idempotency_key",
    "__version__",
]
