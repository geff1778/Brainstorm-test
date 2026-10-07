"""Shared pytest fixtures.

Each test session runs against an isolated temporary ``DATA_DIR`` and the
threading SocketIO async mode so tests do not depend on eventlet.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Configure the environment *before* importing the application.
_TMP_DIR = tempfile.mkdtemp(prefix="brainstorm-tests-")
os.environ.setdefault("ENV", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-1234")
os.environ.setdefault("DATA_DIR", _TMP_DIR)
os.environ.setdefault("SOCKETIO_ASYNC_MODE", "threading")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("GIGACHAT_CREDENTIALS", "")  # force the fallback bank
os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp():
    yield
    import shutil
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


@pytest.fixture()
def app():
    """Fresh application instance with a clean database."""
    from app import create_app
    from app.db import LEADERBOARD_DB, USER_DB, init_databases
    from app.domain.registry import registry
    from app.http_api import admin_routes, cheat_routes, helpers

    for path in (LEADERBOARD_DB, USER_DB):
        if path.exists():
            path.unlink()
    init_databases()
    registry.clear()

    # The rate limiters are process-global; clear them so tests do not leak
    # activation attempts into one another.
    for limiter in (cheat_routes.activation_limiter, admin_routes.activation_limiter,
                    helpers.auth_limiter, helpers.ai_limiter, helpers.api_limiter):
        limiter.reset()

    application = create_app()
    application.config.update(TESTING=True)
    yield application
    registry.clear()


@pytest.fixture()
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture()
def socket_client(app):
    """SocketIO test client bound to ``app``."""
    from app import socketio
    return socketio.test_client(app)


@pytest.fixture()
def socket_factory(app):
    """Factory that builds SocketIO clients, optionally sharing a Flask session.

    Sharing the Flask test client lets a socket inherit the session cookies set
    by ``/api/cheat/activate`` or ``/api/admin/activate`` so privileged events
    can be exercised.
    """
    from app import socketio

    def _make(flask_test_client=None):
        return socketio.test_client(app, flask_test_client=flask_test_client)

    return _make


@pytest.fixture()
def admin_client(app):
    """A Flask test client with an activated admin session."""
    from app.config import settings
    client = app.test_client()
    response = client.post("/api/admin/activate", json={"key": settings.admin_secret_key})
    assert response.status_code == 200, response.get_json()
    return client


@pytest.fixture()
def tester_client(app):
    """A Flask test client with an activated cheat (tester) session."""
    from app.config import settings
    client = app.test_client()
    response = client.post(
        "/api/cheat/activate",
        json={"code": settings.cheat_tester_code, "username": "tester"},
    )
    assert response.status_code == 200, response.get_json()
    return client


def register_and_login(client, username="player1", password="supersecret"):
    """Helper: register a user and return the JSON payload."""
    response = client.post("/api/auth/register", json={"username": username, "password": password})
    assert response.status_code == 200, response.get_json()
    return response.get_json()
