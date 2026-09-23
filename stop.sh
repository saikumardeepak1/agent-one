#!/usr/bin/env bash
# Stop Agent One and its own browser, leaving your everyday Brave alone.
# The pattern matches only the demo profile's path, so a normal Brave can never match it.
set -uo pipefail
pkill -f jev_ultrafast.harness 2>/dev/null && echo "stopped Agent One" || echo "Agent One was not running"
sleep 1
pkill -f AgentOneBrowser 2>/dev/null && echo "stopped the demo browser" || echo "demo browser was not running"
echo "Your own Brave profile was not touched."
