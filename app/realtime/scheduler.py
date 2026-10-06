"""Background task scheduling.

Production runs under eventlet, where blocking the hub with ``time.sleep`` would
stall every other connection.  Tests run under plain threads.  This module hides
the difference behind three helpers so the rest of the code never imports
``eventlet`` directly.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from ..config import settings

# eventlet is only usable when the SocketIO layer actually runs on the eventlet
# hub.  Using ``eventlet.spawn`` while the server runs on plain threads creates
# green threads that never execute, so the decision is driven by configuration
# rather than by whether the package happens to be installed.
_USE_EVENTLET = settings.resolved_async_mode == "eventlet"

if _USE_EVENTLET:  # pragma: no cover - depends on deployment
    import eventlet
else:
    eventlet = None  # type: ignore


def spawn(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run ``func`` in the background and return a handle if one exists."""
    if _USE_EVENTLET:
        return eventlet.spawn(func, *args, **kwargs)
    thread = threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    return thread


def spawn_after(seconds: float, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run ``func`` after ``seconds``."""
    if _USE_EVENTLET:
        return eventlet.spawn_after(seconds, func, *args, **kwargs)
    timer = threading.Timer(seconds, func, args=args, kwargs=kwargs)
    timer.daemon = True
    timer.start()
    return timer


def spawn_periodic(interval: float, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run ``func`` every ``interval`` seconds forever."""
    def _loop() -> None:
        while True:
            sleep(interval)
            try:
                func(*args, **kwargs)
            except Exception:  # noqa: BLE001 - background task must not die
                import logging
                logging.getLogger(__name__).exception("periodic task %s failed", func)

    if _USE_EVENTLET:
        return eventlet.spawn(_loop)
    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return thread


def sleep(seconds: float) -> None:
    """Yield for ``seconds`` without blocking the eventlet hub."""
    if _USE_EVENTLET:
        eventlet.sleep(seconds)
    else:
        time.sleep(seconds)


def is_eventlet() -> bool:
    """Return ``True`` when background tasks run on the eventlet hub."""
    return _USE_EVENTLET
