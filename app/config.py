"""Application configuration.

All runtime configuration is read from environment variables exactly once, at
import time, and validated.  Importing this module never crashes for missing
optional values: safe development defaults are used instead.  However, when
``ENV`` is ``production`` the :func:`Settings.validate` method raises for
insecure defaults so that a misconfigured deployment fails fast instead of
silently shipping the development secret key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

_INSECURE_SECRET_DEFAULTS = {
    "brainstorm-super-secret-key-change-me-in-production",
    "brainstorm-secret-2024",
    "change-me",
    "",
}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the process environment."""

    # ── Runtime ──────────────────────────────────────────────
    env: str = field(default_factory=lambda: os.getenv("ENV", "development").strip().lower())
    debug: bool = field(default_factory=lambda: _env_bool("DEBUG", False))
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 5000))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())

    # ── Security ─────────────────────────────────────────────
    secret_key: str = field(
        default_factory=lambda: os.getenv("SECRET_KEY", "brainstorm-super-secret-key-change-me-in-production")
    )
    admin_secret_key: str = field(default_factory=lambda: os.getenv("ADMIN_SECRET_KEY", "1777"))
    cheat_tester_code: str = field(default_factory=lambda: os.getenv("CHEAT_TESTER_CODE", "19112009"))
    session_cookie_secure: bool = field(default_factory=lambda: _env_bool("SESSION_COOKIE_SECURE", False))
    cors_origins: list[str] = field(default_factory=lambda: _env_list("CORS_ORIGINS", ["*"]))
    trust_proxy: bool = field(default_factory=lambda: _env_bool("TRUST_PROXY", False))
    socketio_async_mode: str = field(default_factory=lambda: os.getenv("SOCKETIO_ASYNC_MODE", "").strip().lower())

    # ── Rate limiting ────────────────────────────────────────
    rate_limit_enabled: bool = field(default_factory=lambda: _env_bool("RATE_LIMIT_ENABLED", True))
    auth_rate_limit: int = field(default_factory=lambda: _env_int("AUTH_RATE_LIMIT", 10))
    auth_rate_window: int = field(default_factory=lambda: _env_int("AUTH_RATE_WINDOW", 300))
    ai_rate_limit: int = field(default_factory=lambda: _env_int("AI_RATE_LIMIT", 20))
    ai_rate_window: int = field(default_factory=lambda: _env_int("AI_RATE_WINDOW", 600))

    # ── GigaChat ─────────────────────────────────────────────
    gigachat_credentials: str = field(default_factory=lambda: os.getenv("GIGACHAT_CREDENTIALS", "").strip())
    gigachat_scope: str = field(default_factory=lambda: os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"))
    gigachat_model: str = field(default_factory=lambda: os.getenv("GIGACHAT_MODEL", "GigaChat"))
    gigachat_verify_ssl: bool = field(default_factory=lambda: _env_bool("GIGACHAT_VERIFY_SSL", False))
    gigachat_timeout: int = field(default_factory=lambda: _env_int("GIGACHAT_TIMEOUT", 30))

    # ── Storage ──────────────────────────────────────────────
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))))

    # ── Game limits ──────────────────────────────────────────
    max_rooms: int = field(default_factory=lambda: _env_int("MAX_ROOMS", 2000))
    room_idle_timeout: int = field(default_factory=lambda: _env_int("ROOM_IDLE_TIMEOUT", 90))
    room_empty_timeout: int = field(default_factory=lambda: _env_int("ROOM_EMPTY_TIMEOUT", 30))

    @property
    def is_production(self) -> bool:
        """True when the app runs under a production environment name."""
        return self.env in {"production", "prod"}

    @property
    def ai_enabled(self) -> bool:
        """True when GigaChat credentials are configured."""
        return bool(self.gigachat_credentials)

    @property
    def resolved_async_mode(self) -> str:
        """SocketIO async mode: explicit override, else eventlet, else threads."""
        if self.socketio_async_mode:
            return self.socketio_async_mode
        try:
            import eventlet  # noqa: F401
            return "eventlet"
        except ImportError:
            return "threading"

    def validate(self) -> None:
        """Fail fast on insecure production configuration."""
        if not self.is_production:
            return
        problems: list[str] = []
        if self.secret_key in _INSECURE_SECRET_DEFAULTS or len(self.secret_key) < 24:
            problems.append("SECRET_KEY must be a long, random value in production")
        if self.admin_secret_key in {"1777", "", "admin"}:
            problems.append("ADMIN_SECRET_KEY must be changed from the default in production")
        if self.cheat_tester_code in {"19112009", ""}:
            problems.append("CHEAT_TESTER_CODE must be changed from the default in production")
        if self.debug:
            problems.append("DEBUG must be disabled in production")
        if problems:
            raise RuntimeError("Insecure production configuration: " + "; ".join(problems))


settings = Settings()
