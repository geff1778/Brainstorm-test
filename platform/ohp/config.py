"""Runtime configuration.

Everything is read from the environment so the platform is portable across
accounts. Secrets are **never** hard-coded and never written to the database or
logs. If a value is missing we fall back to a safe local default rather than
failing at import time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    """Resolved settings for one platform instance."""

    # Where the SQLite database lives. Must be on a durable path.
    db_path: str = field(default_factory=lambda: _env("OHP_DB", str(Path.home() / ".ohp" / "platform.db")))
    # Local agent server (inside this sandbox).
    agent_server_url: str = field(default_factory=lambda: _env("OHP_AGENT_SERVER_URL", "http://127.0.0.1:60000"))
    # OpenHands Cloud app server, used for cross-sandbox delegation.
    cloud_base_url: str = field(default_factory=lambda: _env("OHP_CLOUD_URL", "https://app.all-hands.dev"))
    # Concurrency caps — conservative by default.
    max_parallel: int = field(default_factory=lambda: _env_int("OHP_MAX_PARALLEL", 3))
    lease_seconds: int = field(default_factory=lambda: _env_int("OHP_LEASE_SECONDS", 600))
    task_timeout_s: int = field(default_factory=lambda: _env_int("OHP_TASK_TIMEOUT", 1800))
    poll_interval_s: int = field(default_factory=lambda: _env_int("OHP_POLL_INTERVAL", 5))
    # Reasoning effort handed to the model for delegated conversations.
    reasoning_effort: str = field(default_factory=lambda: _env("OHP_REASONING_EFFORT", "high"))
    default_model: str = field(default_factory=lambda: _env("OHP_MODEL", ""))
    log_level: str = field(default_factory=lambda: _env("OHP_LOG_LEVEL", "INFO"))

    @property
    def session_api_key(self) -> str:
        """Agent-server session key. Read from the environment, never stored."""
        return _env("SESSION_API_KEY")

    @property
    def cloud_api_key(self) -> str:
        """OpenHands Cloud key for delegation."""
        return _env("OPENHANDS_CLOUD_API_KEY") or _env("OPENHANDS_API_KEY")

    def ensure_dirs(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
