"""Application factory.

``create_app`` wires configuration, logging, the database, the HTTP blueprints
and the realtime layer together.  Importing this module has no side effects
beyond creating the (uninitialised) ``SocketIO`` instance, which keeps it safe
for tests and for WSGI servers that import the module more than once.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify
from flask_socketio import SocketIO

from .config import settings
from .db import init_databases
from .http_api.helpers import register_error_handlers
from .logging_setup import configure_logging, get_logger

logger = get_logger(__name__)

#: Created once and initialised per app instance by :func:`create_app`.
socketio = SocketIO()

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(*, init_db: bool = True) -> Flask:
    """Build and configure the Flask application."""
    configure_logging(settings.log_level)
    settings.validate()

    app = Flask(
        __name__,
        template_folder=str(_TEMPLATE_DIR),
        static_folder=str(_STATIC_DIR),
    )
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.session_cookie_secure,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        JSON_SORT_KEYS=False,
    )

    if init_db:
        init_databases()

    _register_blueprints(app)
    register_error_handlers(app)
    _init_socketio(app)

    @app.get("/readyz")
    def readyz():
        """Kubernetes-style readiness endpoint."""
        return jsonify({"status": "ready"})

    logger.info("BrainStorm started (env=%s, async=%s, ai=%s)",
                settings.env, settings.resolved_async_mode, settings.ai_enabled)
    return app


def _register_blueprints(app: Flask) -> None:
    from .http_api import (
        admin_routes,
        auth,
        campaign_routes,
        cheat_routes,
        core,
        leaderboard_routes,
        learn_routes,
        profile_routes,
        rooms,
        ugc_routes,
    )

    app.register_blueprint(core.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(rooms.bp)
    app.register_blueprint(leaderboard_routes.bp)
    app.register_blueprint(ugc_routes.bp)
    app.register_blueprint(campaign_routes.bp)
    app.register_blueprint(learn_routes.bp)
    app.register_blueprint(profile_routes.bp)
    app.register_blueprint(cheat_routes.bp)
    app.register_blueprint(admin_routes.bp)


def _init_socketio(app: Flask) -> None:
    socketio.init_app(
        app,
        cors_allowed_origins=settings.cors_origins,
        async_mode=settings.resolved_async_mode,
        logger=False,
        engineio_logger=False,
    )
    from .realtime.socket_events import register_handlers
    register_handlers(socketio, settings.cheat_tester_code)


def run_dev_server() -> None:
    """Run the development server with the correct async mode."""
    app = create_app()
    socketio.run(
        app,
        host=settings.host,
        port=settings.port,
        debug=settings.debug,
        allow_unsafe_werkzeug=True,
    )
