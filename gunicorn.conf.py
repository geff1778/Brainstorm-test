"""Gunicorn configuration for production (``gunicorn -c gunicorn.conf.py wsgi:app``).

A single eventlet worker is deliberate: live room state lives in process memory
and each Socket.IO session is bound to the worker that accepted it. Scaling out
requires moving that state to Redis (see ROADMAP.md) and enabling sticky
sessions, not simply raising ``workers``.
"""

import os

bind = f"0.0.0.0:{os.getenv('PORT', '5000')}"
worker_class = "eventlet"
workers = 1
worker_connections = 1000
timeout = 120
graceful_timeout = 30
keepalive = 5

# Recycle the worker periodically to release any slow leak.
max_requests = 2000
max_requests_jitter = 200

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()
