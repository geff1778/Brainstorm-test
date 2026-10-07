"""Shared HTTP helpers: authentication decorators and error mapping."""

from __future__ import annotations

import functools
from collections.abc import Callable

from flask import jsonify, request, session

from ..config import settings
from ..errors import (
    AuthError,
    BrainStormError,
    ConflictError,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)
from ..logging_setup import get_logger
from ..security import RateLimiter, client_ip

logger = get_logger(__name__)

#: Rate limiters shared by the HTTP blueprints.  Public names so tests can
#: reset them between cases.
auth_limiter = RateLimiter(settings.auth_rate_limit, settings.auth_rate_window)
ai_limiter = RateLimiter(settings.ai_rate_limit, settings.ai_rate_window)
api_limiter = RateLimiter(600, 60)


def current_username() -> str | None:
    """Return the logged-in username, or ``None``."""
    return session.get("username")


def require_user() -> str | None:
    """Return the username or raise :class:`AuthError`."""
    username = session.get("username")
    if not username:
        raise AuthError("Не авторизован")
    return username


def require_admin() -> None:
    """Raise :class:`AuthError` unless the session is an administrator."""
    if not session.get("is_admin"):
        raise AuthError("Нет доступа")


def request_ip() -> str:
    """Effective client IP for rate limiting and bans."""
    return client_ip(
        request.remote_addr,
        request.headers.get("X-Forwarded-For"),
        trust_proxy=settings.trust_proxy,
    )


def rate_limit(limiter: RateLimiter, *, key_prefix: str = "", by_user: bool = False) -> Callable:
    """Decorator enforcing a rate limit for the current client."""
    def decorator(func: Callable) -> Callable:
        """Wrap ``func`` with the limiter check."""
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            """Enforce the limit, then delegate to the wrapped view."""
            if not settings.rate_limit_enabled:
                return func(*args, **kwargs)
            identity = session.get("username") if by_user else request_ip()
            key = f"{key_prefix}:{identity or 'anon'}"
            if not limiter.allow(key):
                raise RateLimitedError(f"Слишком много запросов. Повторите через {limiter.retry_after(key)} с.")
            return func(*args, **kwargs)
        return wrapper
    return decorator


def error_response(exc: BrainStormError):
    """Map a domain exception to a JSON response with the right status code."""
    if isinstance(exc, AuthError):
        return jsonify({"ok": False, "error": str(exc)}), 401
    if isinstance(exc, NotFoundError):
        return jsonify({"ok": False, "error": str(exc)}), 404
    if isinstance(exc, ConflictError):
        return jsonify({"ok": False, "error": str(exc)}), 409
    if isinstance(exc, RateLimitedError):
        return jsonify({"ok": False, "error": str(exc)}), 429
    if isinstance(exc, ValidationError):
        return jsonify({"ok": False, "error": str(exc)}), 400
    logger.exception("Unhandled application error: %s", exc)
    return jsonify({"ok": False, "error": "Внутренняя ошибка сервера"}), 500


def register_error_handlers(app) -> None:
    """Install JSON error handlers for the whole application."""
    @app.errorhandler(BrainStormError)
    def _handle_domain(exc):  # pragma: no cover - exercised via routes
        return error_response(exc)

    @app.errorhandler(404)
    def _not_found(_exc):
        return jsonify({"ok": False, "error": "Не найдено"}), 404

    @app.errorhandler(405)
    def _method_not_allowed(_exc):
        return jsonify({"ok": False, "error": "Метод не разрешён"}), 405

    @app.errorhandler(500)
    def _server_error(_exc):  # pragma: no cover - defensive
        return jsonify({"ok": False, "error": "Внутренняя ошибка сервера"}), 500
