"""Live JPEG frames from the driven tab, on a CDP socket of its own.

The agent's own commands go through the Browser Harness daemon, which serialises them. Screencast
frames must not queue behind a click, so this opens a second websocket straight to the browser and
only ever reads. It never sends input.
"""

import itertools
import json
import os
import threading
import urllib.request

from websockets.sync.client import connect

from .tab import VIEW_HEIGHT, VIEW_WIDTH

PORT = int(os.environ.get("JEV_CDP_PORT", "9222"))


def browser_socket_url():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=5) as response:
        return json.load(response)["webSocketDebuggerUrl"]


class Screencast(threading.Thread):
    """Holds the most recent frame of one target as base64 JPEG."""

    daemon = True

    def __init__(self, target_id, width=VIEW_WIDTH, height=VIEW_HEIGHT, quality=72):
        super().__init__(name="screencast")
        self.target_id = target_id
        self.size = (width, height, quality)
        self._frame = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ids = itertools.count(1)
        self.error = None

    @property
    def frame(self):
        with self._lock:
            return self._frame

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            self._pump()
        except Exception as error:  # a dead feed must never take the agent down
            self.error = f"{type(error).__name__}: {error}"

    def _pump(self):
        width, height, quality = self.size
        with connect(browser_socket_url(), max_size=None, open_timeout=10) as socket:

            def send(method, session=None, **params):
                message = {"id": next(self._ids), "method": method, "params": params}
                if session:
                    message["sessionId"] = session
                socket.send(json.dumps(message))

            send("Target.attachToTarget", targetId=self.target_id, flatten=True)
            session = None
            while session is None and not self._stop.is_set():
                message = json.loads(socket.recv(timeout=10))
                if message.get("result", {}).get("sessionId"):
                    session = message["result"]["sessionId"]

            send("Page.enable", session)
            send(
                "Page.startScreencast",
                session,
                format="jpeg",
                quality=quality,
                maxWidth=width,
                maxHeight=height,
                everyNthFrame=1,
            )

            while not self._stop.is_set():
                try:
                    message = json.loads(socket.recv(timeout=1))
                except TimeoutError:
                    continue
                if message.get("method") != "Page.screencastFrame":
                    continue
                params = message["params"]
                with self._lock:
                    self._frame = params["data"]
                # Chrome pauses the feed until each frame is acknowledged.
                send("Page.screencastFrameAck", session, sessionId=params["sessionId"])
