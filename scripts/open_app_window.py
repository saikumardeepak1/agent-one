"""Open the Agent One page in the demo browser, in a clean window sized for recording.

The app used to be opened in whatever browser the person already had, which meant recording a
window carrying their bookmarks bar, extensions and other tabs. The demo profile has none of that,
so the same page in the same browser records far better: a blank Brave with one tab.

This is safe because harness.page_targets() excludes the app's own host from every sweep, so the
page opened here is never closed as a stale tab, never mistaken for the airline handoff, and never
the window that gets minimised.
"""

import json
import os
import sys
import time
import urllib.request

from websockets.sync.client import connect

PORT = int(os.environ.get("JEV_CDP_PORT", "9222"))
APP = f"http://127.0.0.1:{os.environ.get('AGENT_ONE_PORT', '8767')}/"

# A fixed size so every take frames identically. On a retina display this records at 2x.
WIDTH = int(os.environ.get("AGENT_ONE_WINDOW_WIDTH", "1440"))
HEIGHT = int(os.environ.get("AGENT_ONE_WINDOW_HEIGHT", "900"))


def endpoint():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=5) as response:
        return json.load(response)["webSocketDebuggerUrl"]


def existing_app_target():
    """Reuse the window if one is already pointed at the app, so reruns do not stack up tabs."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as response:
            for target in json.load(response):
                if target.get("type") == "page" and target.get("url", "").startswith(APP):
                    return target["id"]
    except (OSError, ValueError):
        pass
    return None


def main():
    messages = iter(range(1, 10_000))
    with connect(endpoint(), max_size=None, open_timeout=10) as socket:

        def call(method, **params):
            message_id = next(messages)
            socket.send(json.dumps({"id": message_id, "method": method, "params": params}))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    reply = json.loads(socket.recv(timeout=5))
                except TimeoutError:
                    continue
                if reply.get("id") == message_id:
                    if reply.get("error"):
                        raise RuntimeError(str(reply["error"])[:200])
                    return reply.get("result", {})
            raise TimeoutError(method)

        target = existing_app_target()
        if not target:
            target = call("Target.createTarget", url=APP, newWindow=True)["targetId"]
            time.sleep(0.8)
        window = call("Browser.getWindowForTarget", targetId=target)["windowId"]
        call(
            "Browser.setWindowBounds",
            windowId=window,
            bounds={"windowState": "normal", "left": 40, "top": 40, "width": WIDTH, "height": HEIGHT},
        )
        call("Target.activateTarget", targetId=target)
        print(f"Agent One open in the demo browser at {WIDTH}x{HEIGHT}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # never let a cosmetic step take the launch down
        print(f"Could not open the app window ({type(error).__name__}); open {APP} yourself.",
              file=sys.stderr)
