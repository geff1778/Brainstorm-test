"""Domain level exceptions.

Keeping a small, explicit exception hierarchy lets the HTTP layer map failures
to status codes without inspecting error strings.
"""

from __future__ import annotations


class BrainStormError(Exception):
    """Base class for all application errors."""


class ValidationError(BrainStormError):
    """User supplied input failed validation."""


class AuthError(BrainStormError):
    """Authentication or authorisation failed."""


class NotFoundError(BrainStormError):
    """Requested entity does not exist."""


class ConflictError(BrainStormError):
    """The request conflicts with current state (e.g. duplicate name)."""


class RateLimitedError(BrainStormError):
    """Too many requests from this client."""


class AIUnavailableError(BrainStormError):
    """The AI backend could not produce a usable response."""
