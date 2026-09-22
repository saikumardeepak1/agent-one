#!/usr/bin/env bash
# Launch an isolated Brave with CDP, then run the agent. No Chrome required.
# Deepak's Mac has no Chrome (removed 2026-09-21); Brave is Chromium so the harness attaches.
# A throwaway profile keeps remote debugging away from the everyday browser's logged-in sessions.
set -euo pipefail
PORT=9222
PROFILE="${BRAVE_JEV_PROFILE:-/tmp/brave-jev-profile}"
BRAVE="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

if ! curl -s --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
  echo "Starting isolated Brave on port $PORT (profile: $PROFILE)"
  mkdir -p "$PROFILE"
  "$BRAVE" --remote-debugging-port=$PORT --user-data-dir="$PROFILE" \
    --no-first-run --no-default-browser-check \
    --disable-brave-rewards --disable-brave-wallet \
    --window-size=1280,900 --window-position=40,40 about:blank >/dev/null 2>&1 &
  BRAVE_PID=$!
  for _ in $(seq 1 20); do
    sleep 0.5
    curl -s --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1 && break
  done
fi

BU_CDP_WS=$(curl -s --max-time 5 "http://127.0.0.1:$PORT/json/version" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['webSocketDebuggerUrl'])")
export BU_CDP_WS
echo "Attached: $BU_CDP_WS"
# Front only the throwaway instance so the run is watchable, leaving the everyday Brave alone.
if [ -n "${BRAVE_PID:-}" ]; then
  osascript -e "tell application \"System Events\" to set frontmost of (first process whose unix id is $BRAVE_PID) to true" 2>/dev/null || true
fi

exec uv run --env-file .env "$@"
