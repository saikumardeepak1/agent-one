"""A short-lived CDP session for a page the agent is not driving.

The agent's own commands go through the Browser Harness daemon, which serialises them. Reading a
page the agent has no business steering, such as the Maps panel behind a route comparison, should
not queue behind that, so this opens its own websocket and closes it on the way out.
"""

import itertools
import json
import os
import time
import urllib.request

from websockets.sync.client import connect

PORT = int(os.environ.get("JEV_CDP_PORT", "9222"))

# The size the driven page believes its window to be. Everything past it is simply off-screen, so
# this is what decides how much of a site fits in the demo pane. Bigger means more of the page is
# visible at once and more of it is reachable by a click, at the cost of smaller text on screen.
VIEW_WIDTH = int(os.environ.get("JEV_VIEW_WIDTH", "1440"))
VIEW_HEIGHT = int(os.environ.get("JEV_VIEW_HEIGHT", "1000"))


def endpoint():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=5) as response:
        return json.load(response)["webSocketDebuggerUrl"]


class Tab:
    """Open a tab, read it, close it. Use as a context manager."""

    def __init__(self, url="about:blank", width=VIEW_WIDTH, height=VIEW_HEIGHT, adopt=None):
        self.url = url
        self.size = (width, height)
        self.target = adopt
        self.own = adopt is None
        self._socket = None
        self._session = None
        self._ids = itertools.count(1)

    def __enter__(self):
        self._socket = connect(endpoint(), max_size=None, open_timeout=10).__enter__()
        if self.own:
            self.target = self._call("Target.createTarget", url="about:blank", background=True)["targetId"]
        self._session = self._call("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        width, height = self.size
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=1, mobile=False)
        # Without focus emulation a background tab stops painting, and the live view goes still.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        if self.url != "about:blank":
            self.navigate(self.url)
        return self

    def __exit__(self, *_):
        try:
            if self.own and self.target:
                self._call("Target.closeTarget", targetId=self.target)
        finally:
            self._socket.__exit__(*_)
            self._socket = None

    def _call(self, method, session=None, **params):
        message_id = next(self._ids)
        message = {"id": message_id, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        self._socket.send(json.dumps(message))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                reply = json.loads(self._socket.recv(timeout=10))
            except TimeoutError:
                continue
            if reply.get("id") == message_id:
                if reply.get("error"):
                    raise RuntimeError(str(reply["error"])[:200])
                return reply.get("result", {})
        raise TimeoutError(f"{method} did not answer")

    def call(self, method, **params):
        return self._call(method, session=self._session, **params)

    def navigate(self, url):
        self.call("Page.navigate", url=url)

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if result.get("exceptionDetails"):
            return None
        return result.get("result", {}).get("value")

    def settle(self, expression, timeout=14, every=0.4):
        """Poll an expression until it returns something truthy, or give up and return None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.evaluate(expression)
            if value:
                return value
            time.sleep(every)
        return None


def open_tab(url, width=VIEW_WIDTH, height=VIEW_HEIGHT):
    """Create a tab, point it at a url, and hand it to the caller still open."""
    with Tab(url=url, width=width, height=height) as tab:
        tab.own = False  # closing it is the caller's job now, not this context manager's
        return tab.target


def browser_command(method, **params):
    """A browser-level CDP call, with no page session attached."""
    with connect(endpoint(), max_size=None, open_timeout=10) as socket:
        socket.send(json.dumps({"id": 1, "method": method, "params": params}))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                reply = json.loads(socket.recv(timeout=10))
            except TimeoutError:
                continue
            if reply.get("id") == 1:
                if reply.get("error"):
                    raise RuntimeError(str(reply["error"])[:200])
                return reply.get("result", {})
    raise TimeoutError(method)


def set_window(target_id, minimized):
    """Hide the real browser window, or bring it back.

    Minimising is safe for the demo feed: focus emulation keeps the tab compositing, so the
    screencast keeps arriving at full rate from a window that is not on screen at all. Measured at
    151 frames in 2.5s minimised against 151 normal. Moving the window offscreen does not work on
    macOS, which clamps the position back onto the display.
    """
    window = browser_command("Browser.getWindowForTarget", targetId=target_id)["windowId"]
    state = "minimized" if minimized else "normal"
    browser_command("Browser.setWindowBounds", windowId=window, bounds={"windowState": state})
    if not minimized:
        # Un-minimising alone leaves it behind whatever is in front, so raise it too.
        browser_command("Target.activateTarget", targetId=target_id)
