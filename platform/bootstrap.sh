#!/usr/bin/env bash
#
# bootstrap.sh — make this platform reproducible on a fresh OpenHands runtime.
#
# It is safe to re-run: every step checks before it acts. It never prints or
# stores secrets; it only reads the session key the runtime already provides.
#
# Usage:
#   ./bootstrap.sh            # set up and verify
#   ./bootstrap.sh --drain    # set up, then run the queue once
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. Python and the package ---------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 is required"
log "python: $(python3 --version 2>&1)"

if ! python3 -c "import pytest" >/dev/null 2>&1; then
  log "installing dev dependencies (pytest, ruff)"
  python3 -m pip install --quiet --user pytest ruff || \
    log "WARN: could not install dev dependencies; tests will not run"
fi

if ! python3 -c "import ohp" >/dev/null 2>&1; then
  log "installing the ohp package (editable)"
  python3 -m pip install --quiet --user -e . || die "pip install failed"
fi

# --- 2. Session key ---------------------------------------------------------
if [ -z "${SESSION_API_KEY:-}" ]; then
  KEY_FILE="${OH_SESSION_API_KEY_PATH:-$HOME/.openhands/agent-canvas/api-key.txt}"
  if [ -f "$KEY_FILE" ]; then
    export SESSION_API_KEY="$(cat "$KEY_FILE")"
    log "session key loaded from $KEY_FILE"
  else
    log "WARN: SESSION_API_KEY is not set and $KEY_FILE was not found"
    log "WARN: the agent server will be reported as unavailable"
  fi
else
  log "session key present in the environment"
fi

# --- 3. Model ---------------------------------------------------------------
if [ -z "${OHP_MODEL:-}" ]; then
  # Prefer the model the runtime's own settings point at; fall back to the
  # managed model that ships with hosted runtimes.
  DETECTED="$(python3 - <<'PY' 2>/dev/null || true
import json, os, urllib.request
url = os.environ.get("OHP_AGENT_SERVER_URL", "http://127.0.0.1:60000") + "/api/settings"
req = urllib.request.Request(url, headers={"X-Session-API-Key": os.environ.get("SESSION_API_KEY", "")})
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        print(json.load(r).get("agent_settings", {}).get("llm", {}).get("model", ""))
except Exception:
    pass
PY
)"
  if [ -n "$DETECTED" ]; then
    export OHP_MODEL="$DETECTED"
    log "model detected from agent settings: $OHP_MODEL"
  fi
fi

# --- 4. Database ------------------------------------------------------------
export OHP_DB="${OHP_DB:-$ROOT/data/platform.db}"
export OHP_AGENT_SERVER_URL="${OHP_AGENT_SERVER_URL:-http://127.0.0.1:60000}"
mkdir -p "$(dirname "$OHP_DB")"
python3 -m ohp init >/dev/null && log "database ready at $OHP_DB"

# --- 5. Verify --------------------------------------------------------------
log "running doctor"
python3 -m ohp doctor || log "WARN: doctor reported problems"

log "running the test suite"
if python3 -m pytest -q >/dev/null 2>&1; then
  log "tests passed"
else
  log "WARN: tests failed"
fi

# --- 6. Optional drain ------------------------------------------------------
if [ "${1:-}" = "--drain" ]; then
  log "draining the queue"
  python3 -m ohp run --drain --max 5
fi

log "done"
log "next: python3 -m ohp submit \"<your request>\" --project <name>"
