#!/usr/bin/env bash
# Stop Agent One and its throwaway browser, leaving your own Brave alone.
# The pattern matches only the demo's temp profile path, so a normal Brave can never match it.
set -uo pipefail
pkill -f jev_ultrafast.harness 2>/dev/null && echo "stopped Agent One" || echo "Agent One was not running"
sleep 1
pkill -f brave-jev-profile 2>/dev/null && echo "stopped the demo browser" || echo "demo browser was not running"
echo "Your own Brave profile was not touched."
