"""Small helper used by ``start.bat`` on Windows.

The launcher shells out to this script for three one-line answers so the batch
file itself stays free of fragile Python parsing:

    python _check.py pyver            -> "OK 3.12.4" or "OLD 3.9.1"
    python _check.py gigachat <cred>  -> "OK" or "FAIL: <reason>"
    python _check.py ip               -> best-effort LAN IP, else "127.0.0.1"

It is intentionally dependency free and never raises: the batch file reads
stdout, so any error must still produce a usable line.
"""

from __future__ import annotations

import socket
import sys


def _python_version() -> str:
    """Return ``OK <version>`` when the interpreter is 3.10+, else ``OLD``."""
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info[2]}"
    return f"OK {version}" if (major, minor) >= (3, 10) else f"OLD {version}"


def _gigachat_status(credentials: str) -> str:
    """Try a trivial GigaChat handshake and report ``OK`` or a failure reason."""
    if not credentials:
        return "FAIL: no credentials"
    try:
        from gigachat import GigaChat
    except ImportError:
        return "FAIL: gigachat not installed"
    try:
        with GigaChat(credentials=credentials, verify_ssl_certs=False, timeout=15) as client:
            client.get_models()
        return "OK"
    except Exception as exc:  # noqa: BLE001 - any failure means "not usable"
        return f"FAIL: {str(exc)[:80]}"


def _local_ip() -> str:
    """Best-effort LAN address without sending traffic to the internet."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def main(argv: list[str]) -> int:
    """Dispatch to the requested check and print a single result line."""
    command = argv[1] if len(argv) > 1 else ""
    if command == "pyver":
        print(_python_version())
    elif command == "gigachat":
        print(_gigachat_status(argv[2] if len(argv) > 2 else ""))
    elif command == "ip":
        print(_local_ip())
    else:
        print("usage: _check.py {pyver|gigachat <credentials>|ip}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
