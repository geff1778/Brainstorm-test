#!/usr/bin/env python3
"""WSGI entry point for production servers (gunicorn, Render, Docker).

Usage::

    gunicorn -k eventlet -w 1 wsgi:app
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

from app import create_app, socketio  # noqa: E402  (env must load first)

app = create_app()

# gunicorn imports ``app``; exposing ``socketio`` lets eventlet deployments use
# ``socketio.run``-style workers if required.
__all__ = ["app", "socketio"]

if __name__ == "__main__":  # pragma: no cover - manual invocation only
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
