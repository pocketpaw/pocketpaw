#!/usr/bin/env bash
# verify.sh — launch / doctor / cleanup helper for the verify-pocketpaw skill.
# Starts ONE dashboard (`uv run pocketpaw --port N`, N >= 8889) from the repo
# root, records the wrapper pid plus the port uvicorn actually bound under
# .verify-evidence/current/, answers "is this instance drivable?", and tears
# down only the pid it recorded. Evidence dirs (.verify-evidence/<run-id>/)
# are never touched by cleanup. Invariant: the port is read from the server
# log, not assumed, because `--port` silently falls back to port+1 when busy.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
EVID="$REPO/.verify-evidence"
CUR="$EVID/current"
PORT="${PORT:-8889}"

listeners() { lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true; }
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" 2>/dev/null || echo 000; }

launch() {
  if [ -f "$CUR/pid" ] && kill -0 "$(cat "$CUR/pid")" 2>/dev/null; then
    echo "refusing: a verify server is already recorded (pid $(cat "$CUR/pid")). Run cleanup first."; exit 2
  fi
  if [ "$PORT" -le 8888 ]; then echo "refusing: PORT must be 8889 or higher (other sessions hold 8888)."; exit 2; fi
  if [ -n "$(listeners "$PORT")" ]; then
    echo "refusing: port $PORT is held by a process this run did not start. Pick PORT=<other>."; exit 2
  fi
  [ -x "$REPO/.venv/bin/pocketpaw" ] || { echo "refusing: no .venv/bin/pocketpaw; run 'uv sync --dev' in $REPO first."; exit 2; }
  RUN_ID="$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$CUR" "$EVID/$RUN_ID"
  LOG="$EVID/$RUN_ID/server.log"
  cd "$REPO"
  # BROWSER=/usr/bin/true: python's webbrowser honours it, so the dashboard does
  # not pop a tab on the user's desktop. CLI path: the bundled Claude CLI goes
  # stale; the system one is what the Claude Code backend should drive.
  BROWSER=/usr/bin/true \
  POCKETPAW_CLAUDE_SDK_CLI_PATH="${POCKETPAW_CLAUDE_SDK_CLI_PATH:-$(command -v claude || true)}" \
    nohup uv run pocketpaw --port "$PORT" > "$LOG" 2>&1 &
  echo $! > "$CUR/pid"; echo "$RUN_ID" > "$CUR/run-id"
  for _ in $(seq 1 90); do
    BOUND="$(grep -oE 'Uvicorn running on http://[0-9.]+:[0-9]+' "$LOG" 2>/dev/null | tail -1 | sed 's/.*://' || true)"
    if [ -n "$BOUND" ] && [ "$(http_code "http://127.0.0.1:$BOUND/")" = "200" ]; then
      echo "$BOUND" > "$CUR/port"
      [ "$BOUND" = "$PORT" ] || echo "note: requested $PORT, uvicorn bound $BOUND (port fallback); recorded $BOUND"
      echo "ready: http://127.0.0.1:$BOUND (pid $(cat "$CUR/pid"), run $RUN_ID, evidence $EVID/$RUN_ID)"; return 0
    fi
    kill -0 "$(cat "$CUR/pid")" 2>/dev/null || { echo "server exited early; see $LOG"; rm -rf "$CUR"; exit 1; }
    sleep 1
  done
  echo "not ready after 90s; see $LOG (run cleanup before retrying)"; exit 1
}

doctor() {
  [ -f "$CUR/pid" ] && [ -f "$CUR/port" ] || { echo "not drivable here: no recorded server (run launch)"; exit 1; }
  PID="$(cat "$CUR/pid")"; P="$(cat "$CUR/port")"
  kill -0 "$PID" 2>/dev/null || { echo "not drivable here: recorded pid $PID is gone"; exit 1; }
  OWNERS="$(listeners "$P")"
  [ -n "$OWNERS" ] || { echo "not drivable here: nothing listens on $P"; exit 1; }
  for o in $OWNERS; do
    [ "$o" = "$PID" ] && continue
    [ "$(ps -o ppid= -p "$o" | tr -d ' ')" = "$PID" ] && continue
    echo "not drivable here: port $P is held by pid $o, not ours"; exit 1
  done
  ROOT="$(http_code "http://127.0.0.1:$P/")"
  [ "$ROOT" = "200" ] || { echo "not drivable here: GET / -> $ROOT"; exit 1; }
  HEALTH="$(curl -s --max-time 5 "http://127.0.0.1:$P/api/v1/health" 2>/dev/null || echo '{}')"
  # First "status" is the summary; later ones belong to individual checks.
  STATUS="$(printf '%s' "$HEALTH" | grep -oE '"status":"[a-z]+"' | head -1 | sed 's/.*:"//;s/"//')"
  echo "server: pid $PID owns port $P, GET / -> $ROOT, /api/v1/health status=${STATUS:-unknown}"
  echo "run: $(cat "$CUR/run-id"), evidence $EVID/$(cat "$CUR/run-id")"
  echo "drivable: all mapped features are read-only UI and render without an API key (health 'degraded' = no Anthropic key, expected). Shares ~/.pocketpaw with the user's live instance: do not send, delete, run, or save."
}

cleanup() {
  [ -f "$CUR/pid" ] || { echo "nothing recorded; nothing killed"; exit 0; }
  PID="$(cat "$CUR/pid")"; P="$(cat "$CUR/port" 2>/dev/null || echo "$PORT")"
  kill "$PID" 2>/dev/null || true   # SIGTERM to the uv wrapper propagates to the uvicorn child
  for _ in $(seq 1 15); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  if kill -0 "$PID" 2>/dev/null; then kill -9 "$PID" 2>/dev/null || true; fi
  for o in $(listeners "$P"); do
    [ "$(ps -o ppid= -p "$o" 2>/dev/null | tr -d ' ')" = "$PID" ] && kill -9 "$o" 2>/dev/null || true
  done
  sleep 1
  LEFT="$(listeners "$P")"
  [ -z "$LEFT" ] || echo "port $P still held by $LEFT (not started by us; left alone)"
  rm -rf "$CUR"
  echo "cleaned: pid $PID stopped, port $P $( [ -z "$LEFT" ] && echo free || echo busy ); evidence kept under $EVID/"
}

case "${1:-}" in
  launch) launch;; doctor) doctor;; cleanup) cleanup;;
  *) echo "usage: verify.sh launch|doctor|cleanup   (env: PORT >= 8889, POCKETPAW_CLAUDE_SDK_CLI_PATH)"; exit 2;;
esac
