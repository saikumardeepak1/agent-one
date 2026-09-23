"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon

from .tab import VIEW_HEIGHT, VIEW_WIDTH
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


# CDP -32001. The session is bound to one target, and a handoff can take that target away:
# "Continue to book" can navigate cross-origin in place, which swaps the target out from under us.
# Every later call then failed with this forever, because nothing re-attached, and the run died on
# the last step with the fare already chosen.
SESSION_LOST = "Session with given id not found"


class Browser:
    def __init__(self, url):
        ensure_daemon()
        # Its own window, so the demo can minimise the driven tab without hiding anything else the
        # person has open in this browser, such as the Agent One page itself.
        self.target = cdp(
            "Target.createTarget", url="about:blank", background=True, newWindow=True
        )["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=VIEW_WIDTH, height=VIEW_HEIGHT, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def adopt(self, target_id):
        """Follow a handoff into another tab, e.g. the airline page Google opens on Continue."""
        self.target = target_id
        self.session = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=VIEW_WIDTH, height=VIEW_HEIGHT, deviceScaleFactor=1, mobile=False)
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.after_input = None

    def _recover_session(self):
        """Re-attach after the session is dropped, so a handoff cannot end the run.

        Re-attaching to the same target covers the usual case, where the session went but the tab
        is still there. If the target itself is gone, adopt whichever page is live instead, never
        the Agent One page, which is the one tab that must not be driven or minimised.
        """
        if getattr(self, "_recovering", False):
            return False
        self._recovering = True
        try:
            try:
                self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
                return True
            except Exception:
                pass
            try:
                targets = cdp("Target.getTargets")["targetInfos"]
            except Exception:
                return False
            app = f":{os.environ.get('AGENT_ONE_PORT', '8767')}/"
            live = [
                t for t in targets
                if t.get("type") == "page" and app not in t.get("url", "")
                and not t.get("url", "").startswith(("devtools://", "chrome://"))
            ]
            if not live:
                return False
            self.adopt(live[-1]["targetId"])
            return True
        finally:
            self._recovering = False

    def _retry_if_session_lost(self, run):
        try:
            return run()
        except Exception as error:
            if SESSION_LOST not in str(error) or not self._recover_session():
                raise
            return run()

    def call(self, method, **params):
        return self._retry_if_session_lost(lambda: cdp(method, session_id=self.session, **params))

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return self._retry_if_session_lost(
                    lambda: browser_operation(
                        {"operation": "observe", "session": self.session, "screenshot": screenshot}
                    )
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = self._retry_if_session_lost(
            lambda: browser_operation(
                {"operation": "act", "session": self.session, "action": action, "text": text}
            )
        )
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              const hit=document.elementFromPoint(x,y);
              if (!e.contains(hit)) {
                // Some sites paint a sibling layer over the semantic control. Google Flights result rows
                // do this, so elementFromPoint returns a label drawn inside the row rather than the row's
                // own role=link node. Accept a cover only when it sits entirely inside the target's box:
                // a banner, dialog or consent overlay extends past the target and is still rejected.
                const h=hit?.getBoundingClientRect();
                if (!h || h.left<r.left-1 || h.top<r.top-1 || h.right>r.right+1 || h.bottom>r.bottom+1) return null;
                const modal='[role="dialog"],[aria-modal="true"]';
                if (hit.closest(modal) !== e.closest(modal)) return null;
              }
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                # Move before pressing. Google Flights result rows are hover-gated: with no pointer
                # ever moved onto them, the press and release land but the row never navigates, so
                # the agent reads an unchanged page and gives up on a click that looked fine.
                # Puppeteer and Playwright both move first for this reason.
                call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
