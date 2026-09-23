#!/usr/bin/env bash
# Agent One. Brave runs offscreen; the live feed is streamed into the app, so there is exactly
# one window to record and no tab to hunt for.
set -euo pipefail
PORT=9222
APP_PORT="${AGENT_ONE_PORT:-8767}"
# A profile of its own, because Brave refuses remote debugging on the default one:
# "DevTools remote debugging requires a non-default data directory." It lives in Application
# Support rather than /tmp so signing in once actually sticks; /tmp is wiped on reboot.
PROFILE="${AGENT_ONE_PROFILE:-$HOME/Library/Application Support/AgentOneBrowser}"
BRAVE="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
# Park the window past the right edge of the widest attached display.
OFFSET="${BRAVE_OFFSCREEN:-6000}"

if ! curl -s --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
  echo "Starting Brave offscreen at x=$OFFSET (profile: $PROFILE)"
  mkdir -p "$PROFILE"
  "$BRAVE" --remote-debugging-port=$PORT --user-data-dir="$PROFILE" \
    --no-first-run --no-default-browser-check \
    --disable-brave-rewards --disable-brave-wallet \
    --window-size=1120,780 --window-position=$OFFSET,80 \
    --disable-backgrounding-occluded-windows \
    --disable-renderer-backgrounding \
    --disable-background-timer-throttling \
    about:blank >/dev/null 2>&1 &
  for _ in $(seq 1 20); do
    sleep 0.5
    curl -s --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1 && break
  done
fi

BU_CDP_WS=$(curl -s --max-time 5 "http://127.0.0.1:$PORT/json/version" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['webSocketDebuggerUrl'])")
# How large the driven page believes its window to be. Anything past this is off-screen, so it
# decides how much of a site fits in the demo pane. Larger shows more of the page and makes more of
# it clickable; smaller makes the text bigger on camera.
export JEV_VIEW_WIDTH="${JEV_VIEW_WIDTH:-1440}"
export JEV_VIEW_HEIGHT="${JEV_VIEW_HEIGHT:-1000}"
export BU_CDP_WS JEV_CDP_PORT=$PORT AGENT_ONE_PORT=$APP_PORT
echo "Brave attached. Agent One on http://127.0.0.1:$APP_PORT"
cat <<'NOTE'

  Heads up: the demo drives its own Brave profile (AgentOneBrowser), and macOS gives the Dock
  icon to whichever Brave is already running. While this is up, clicking Brave in the Dock may
  show you that profile rather than your own. Your real profile is untouched either way.
  Sign in to that window once and it will remember you on every later run.
  Get your own browser back with:  ./stop.sh

NOTE

exec uv run --env-file .env python -m jev_ultrafast.harness
