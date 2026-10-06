#!/usr/bin/env python3
"""Local development entry point.

Usage::

    python run.py

Loads ``.env`` (creating it from ``.env.example`` when absent), then starts the
development server.  For production use gunicorn via ``wsgi.py``.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BANNER_WIDTH = 52


def _configure_encoding() -> None:
    """Force UTF-8 so Cyrillic output is not mangled on Windows consoles."""
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")


def _load_env() -> None:
    """Load ``.env``, seeding it from the example on first run."""
    from dotenv import load_dotenv

    env_path = BASE_DIR / ".env"
    example_path = BASE_DIR / ".env.example"
    if not env_path.exists() and example_path.exists():
        env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
        print("Created .env from .env.example — review it before deploying.")
    load_dotenv(env_path, override=False)


def _local_ip() -> str:
    """Best-effort detection of the LAN IP for the startup banner."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _print_banner(port: int, ip: str, backend: str) -> None:
    """Print a boxed startup summary."""
    def row(text: str) -> str:
        """Pad ``text`` to a fixed-width banner row."""
        return f"|  {text.ljust(BANNER_WIDTH)}|"

    line = "+" + "-" * BANNER_WIDTH + "+"
    print()
    print(line)
    print(row("🧠  BRAINSTORM — server started"))
    print(line)
    print(row(f"Local:   http://localhost:{port}"))
    print(row(f"Network: http://{ip}:{port}"))
    print(row(f"AI:      {backend}"))
    print(line)
    print(row("Press Ctrl+C to stop"))
    print(line)
    print()


def main() -> int:
    """Entry point: load env, print the banner and serve."""
    _configure_encoding()
    _load_env()

    from app import run_dev_server
    from app.config import settings
    from app.services.ai_client import active_backend

    backend = active_backend()
    if not settings.ai_enabled:
        print("WARNING: GIGACHAT_CREDENTIALS is not set — the fallback question bank will be used.")
    _print_banner(settings.port, _local_ip(), backend)
    run_dev_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
