"""Logging configuration.

A single place that configures the root logger so that ``run.py``, ``wsgi.py``
and the test-suite all produce identically formatted output.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False

_FORMAT = "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"

#: Chatty third party loggers that add noise without value.
_QUIET_LOGGERS = ("engineio", "socketio", "urllib3", "werkzeug", "eventlet", "gigachat")


def configure_logging(level: str = "INFO", stream=None) -> None:
    """Configure the root logger once for the whole process."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    resolved = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=resolved,
        format=_FORMAT,
        datefmt=_DATEFMT,
        handlers=[logging.StreamHandler(stream or sys.stdout)],
        force=True,
    )
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a module logger, configuring logging on first use."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
