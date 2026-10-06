"""Core routes: index page and health probe."""

from __future__ import annotations

import time

from flask import Blueprint, jsonify, render_template

from ..config import settings
from ..db import db_health
from ..domain.registry import registry
from ..services.ai_client import active_backend

bp = Blueprint("core", __name__)

APP_VERSION = "2.0.0"


@bp.get("/")
def index():
    """Render the single-page application shell."""
    return render_template("index.html")


@bp.get("/health")
def health():
    """Liveness/readiness probe used by orchestrators."""
    databases = db_health()
    healthy = all(value == "ok" for value in databases.values())
    return jsonify({
        "status": "ok" if healthy else "degraded",
        "version": APP_VERSION,
        "env": settings.env,
        "ts": time.time(),
        "ai": active_backend(),
        "rooms": len(registry),
        "databases": databases,
    }), (200 if healthy else 503)
