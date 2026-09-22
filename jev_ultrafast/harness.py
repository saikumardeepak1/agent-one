"""Jev Flash: a one-tool agent harness. Type a sentence, watch it book the cheapest seat.

The split is the point. A small text model reads the sentence into a typed itinerary, Jev chooses
every operation and target after that, and code executes. The run stops at the passenger details
step: nothing is ever typed into a personal or payment field.
"""

import atexit
import base64
import datetime
import json
import os
import secrets
import statistics
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import model, picker, router
from .agent import Agent
from .screencast import Screencast
from .tab import Tab, set_window

ROOT = Path(__file__).parent
PORT = int(os.environ.get("AGENT_ONE_PORT", "8767"))
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = secrets.token_urlsafe(32)

STEPS = [
    ("intent", "Read request"),
    ("open", "Open Google Maps"),
    ("modes", "Read every mode"),
    ("rank", "Jev ranks them"),
    ("choose", "You choose one"),
]

STEPS = [
    ("intent", "Understand request"),
    ("open", "Open Google Flights"),
    ("oneway", "Set trip type"),
    ("origin", "Set origin"),
    ("destination", "Set destination"),
    ("date", "Set date"),
    ("search", "Search flights"),
    ("select", "Pick cheapest fare"),
    ("details", "Reach passenger details"),
]

INTENT = """Read the traveller's sentence and return the itinerary as JSON with exactly these keys:
origin, destination, date, return_date, origin_field, destination_field.
origin and destination are the city names as a person would say them, e.g. "Portland, Oregon".
origin_field and destination_field are how Google Flights shows that city once resolved, which is
usually the bare city name, e.g. "Portland".
date is the departure date as YYYY-MM-DD, or null when the sentence names no date at all.
return_date is the date of the journey home as YYYY-MM-DD, or null when no return is named. Resolve
relative dates against today's date, which is given to you. If the sentence gives no year, choose the
next occurrence of that date in the future.
Return no commentary. If a city is missing from the sentence, return only
{"error": "..."} where the value is one short sentence addressed to the traveller naming what
you still need, for example "Tell me which city you are flying from and on what date."
"""

LOCK = threading.Lock()
STATE = {}
AGENT = None
FEED = None


def blank_state(steps=None):
    return {
        "phase": "idle",
        "messages": [
            {
                "role": "agent",
                "text": "Jev Flash is ready. I book one thing: the cheapest one-way fare. "
                "Tell me where you are going and when.",
            }
        ],
        "steps": [{"id": key, "label": label, "status": "pending"} for key, label in (steps or STEPS)],
        "tool": None,
        "timeline": [],
        "elapsed_ms": 0,
        "running": False,
        "itinerary": None,
        "stats": {},
        "stopped": False,
        "error": None,
    }


def say(role, text):
    STATE["messages"].append({"role": role, "text": text})


def mark(step_id, status="done"):
    for step in STATE["steps"]:
        if step["id"] == step_id:
            # Steps latch: the search form disappears on the booking page, and finished work
            # must not appear to un-happen just because its field is no longer on screen.
            if step["status"] != "done":
                step["status"] = status
            return


def activate(step_id):
    for step in STATE["steps"]:
        if step["id"] == step_id and step["status"] == "pending":
            step["status"] = "active"


def parse_intent(command, need_date=True):
    today = datetime.date.today().isoformat()
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TEXT_MODEL_API_KEY is not set")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    started = time.perf_counter()
    # Small models occasionally emit JSON that the provider's own validator rejects outright.
    # That is a model flake, not a bad request, so give it a couple more tries before giving up.
    for attempt in range(3):
        try:
            result = model.post_json(base + "/chat/completions", key, intent_body(command, today))
            break
        except RuntimeError as error:
            if "json_validate_failed" in str(error) and attempt < 2:
                continue
            raise
    latency = round((time.perf_counter() - started) * 1000)
    return read_itinerary(result, latency, need_date)


def intent_body(command, today):
    reasoning = {} if os.environ.get("TEXT_MODEL_REASONING") == "omit" else {"reasoning": {"enabled": False}}
    return {
        "model": os.environ.get("TEXT_MODEL", "openai/gpt-oss-20b"),
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
        **reasoning,
        "messages": [
            {"role": "system", "content": INTENT},
            {"role": "user", "content": json.dumps({"today": today, "request": command})},
        ],
    }


def read_itinerary(result, latency, need_date=True):
    try:
        parsed = json.loads(result["choices"][0]["message"]["content"])
    except (ValueError, KeyError, IndexError):
        raise ValueError(
            "Could not read that as a trip. Try: book the cheapest flight from Portland "
            "to Denver on March 3"
        ) from None
    if parsed.get("error"):
        raise ValueError(str(parsed["error"])[:200])
    try:
        itinerary = {
            "origin": str(parsed["origin"])[:80],
            "destination": str(parsed["destination"])[:80],
            "origin_field": str(parsed.get("origin_field") or parsed["origin"]).split(",")[0][:60],
            "destination_field": str(parsed.get("destination_field") or parsed["destination"]).split(",")[0][:60],
            "date": None,
            "pretty": "",
            "short": "",
            "return_pretty": "",
            "latency_ms": latency,
        }
    except (KeyError, TypeError):
        raise ValueError("That sentence is missing a city.") from None
    try:
        date = datetime.date.fromisoformat(parsed["date"])
    except (KeyError, TypeError, ValueError):
        if need_date:
            raise ValueError("That sentence is missing a departure date.") from None
        return itinerary
    if date < datetime.date.today():
        raise ValueError(f"{date.strftime('%B %-d, %Y')} is in the past.")
    itinerary.update(date=date.isoformat(), pretty=date.strftime("%B %-d, %Y"),
                     short=date.strftime("%a, %b %-d"))
    try:
        back = datetime.date.fromisoformat(parsed["return_date"])
        itinerary["return_pretty"] = back.strftime("%B %-d, %Y")
    except (KeyError, TypeError, ValueError):
        pass
    return itinerary


APP_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}


def page_targets():
    """Every page tab in the demo browser, except the Agent One page itself.

    Brave is the only browser on this machine, so clicking the app's link hands it to the running
    demo instance. That page must never be swept as a stale tab, never be mistaken for the airline
    handoff, and never decide which window gets minimised.

    Brave also answers /json/version before /json/list is ready, so a fresh launch resets the first
    call or two. Retry briefly rather than letting a cold start take the whole server down.
    """
    port = os.environ.get("JEV_CDP_PORT", "9222")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
                return [
                    x
                    for x in json.load(response)
                    if x.get("type") == "page"
                    and urlparse(x.get("url", "")).netloc not in APP_HOSTS
                ]
        except (OSError, ValueError):
            if attempt == 3:
                raise
            time.sleep(0.6)
    return []


def cdp_http(path, method="GET"):
    port = os.environ.get("JEV_CDP_PORT", "9222")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


def ensure_window():
    """A browser with no tabs left cannot host a background target, and the daemon then times out.

    The agent adopts the airline tab Google opens, so teardown closes that one and the original
    search tab is left behind; close enough of those and Brave ends up with no window at all. Keep
    one anchor tab alive so the next run always has somewhere to open.
    """
    if not page_targets():
        cdp_http("/json/new?about:blank", method="PUT")
        time.sleep(0.6)


def log_row(who, what, ms=None, conf=None):
    """One line for the log drawer. Caller holds LOCK."""
    started = STATE.get("started_at")
    STATE.setdefault("timeline", []).append(
        {
            "t": round((time.perf_counter() - started) * 1000) if started else 0,
            "who": who,
            "what": what[:80],
            "ms": ms,
            "conf": round(conf, 3) if isinstance(conf, (int, float)) else None,
        }
    )


def hide_browser(minimized=True):
    """Keep the driven window off the screen, or hand it back to the traveller.

    Only ever the window holding the tab the agent drives. Minimising whatever happened to be first
    in the list could hide the window the person is watching the app in.
    """
    target = None
    if AGENT is not None and getattr(AGENT, "browser", None) is not None:
        target = AGENT.browser.target
    elif FEED is not None:
        target = FEED.target_id
    if not target:
        # Nothing is being driven, so there is no window of ours to hide. The anchor window is
        # left alone on purpose: Brave may have put the Jev Flash page in it, and minimising that
        # is how the app went blank.
        return
    try:
        set_window(target, minimized)
    except Exception:
        pass


def sweep_stale():
    """Close tabs left over from earlier runs.

    Brave restores the previous session on launch, so without this every run adds tabs that are
    never collected. Nine live tabs was enough to make the daemon time out mid-run. The profile is
    a throwaway used only by this app, so everything in it is ours to close.
    """
    for target in page_targets():
        try:
            cdp_http(f"/json/close/{target['id']}")
        except Exception:
            pass
    # Close everything, then put back one blank anchor. Keeping a real page as the anchor left a
    # stale airline window sitting on screen between takes.
    ensure_window()


def airline_handoff(known_ids):
    """Google hands a booking off by opening the airline in a new tab. That tab is the finish line."""
    for target in page_targets():
        host = urlparse(target.get("url", "")).hostname or ""
        if target["id"] not in known_ids and host and "google.com" not in host:
            return target
    return None


def build_goal(trip, url=""):
    """Two short goals rather than one long one.

    The goal is repeated inside every question's instructions, so a goal carrying booking-stage
    clauses measurably degrades the early search-form decisions. Phase one is the wording already
    proven to reach the booking page; phase two swaps in only once that page is open.
    """
    if "/travel/flights/booking" in url:
        return (
            "Continue to book the cheapest fare with the airline. The Continue button sits below "
            "the fare list, so SCROLL_DOWN until a target whose label starts with 'Continue to "
            "book' is offered, then click the cheapest one. Never enter personal or payment details."
        )
    party = trip.get("party", 1)
    who = "one adult" if party == 1 else f"{party} adults"
    kind = "one-way" if trip.get("trip_type", "one_way") == "one_way" else "round trip"
    when = trip["pretty"] + (f" returning {trip['return_pretty']}" if trip.get("return_pretty") else "")
    return (
        f"Find {kind} flights from {trip['origin']} to {trip['destination']} on {when}, "
        f"for {who} in economy. After the flight results appear, select the flight with the "
        "lowest price. Stop when the booking options for that flight are visible. "
        "Never enter personal or payment details."
    )


def read_progress(page, trip):
    """Derive step completion from observed page state, never from the model's own claims."""
    url = page["url"]
    text = page.get("text", "")
    values = {a["label"].strip(): a.get("value") for a in page["actions"]}
    wanted = "One way" if trip.get("trip_type", "one_way") == "one_way" else "Round trip"
    if values.get(f"Change ticket type. {wanted}") == wanted:
        mark("oneway")
    if values.get("Where from?"):
        mark("origin")
    if values.get("Where to?"):
        mark("destination")
    if values.get("Departure"):
        mark("date")
    if "/travel/flights/search" in url:
        mark("oneway"), mark("origin"), mark("destination"), mark("date"), mark("search")
    if "/travel/flights/booking" in url or "Selected flights" in text:
        mark("search"), mark("select")
    host = urlparse(url).hostname or ""
    # An account wall is a wrong turn, not the details step. Only a real airline or booking host counts.
    offsite = host not in ("", "www.google.com", "accounts.google.com")
    details = any(
        phrase in text
        for phrase in (
            "Passenger 1", "Traveler 1", "Traveller 1", "Who's flying", "Passenger details",
            "Contact details", "Sign in to book", "Sign in to continue", "Enter passenger",
        )
    )
    if offsite:
        mark("select"), mark("details")
    elif details and "/travel/flights/booking" in url:
        mark("details")


def summarise(state, trip):
    jev = [d["latency_ms"] for d in state["decisions"]] or [0]
    tokens = sum(
        d.get("usage", {}).get("input_tokens", 0) or d.get("usage", {}).get("prompt_tokens", 0)
        for d in state["decisions"]
    )
    return {
        "actions": len(state["history"]),
        "decisions": len(state["decisions"]),
        "median_ms": round(statistics.median(jev)),
        "jev_ms": sum(jev),
        "text_calls": len(state["text_calls"]) + 1,
        "cost_usd": round(tokens * 0.042 / 1_000_000, 5),
        "url": state["page"]["url"],
    }


def fail(error):
    with LOCK:
        STATE["phase"] = "error"
        STATE["running"] = False
        if STATE.get("started_at"):
            STATE["elapsed_ms"] = round((time.perf_counter() - STATE["started_at"]) * 1000)
        STATE["error"] = f"{type(error).__name__}: {error}"
        say("agent", str(error)[:300])


def run_task(command):
    """Jev routes the sentence first; the matching tool runs second.

    Routing is a fixed option set where being wrong is expensive, so Jev owns it and the text model
    is left with the one job it is needed for: turning place names and dates into strings.
    """
    with LOCK:
        STATE.update(blank_state())
        STATE["phase"] = "parsing"
        STATE["running"] = True
        STATE["started_at"] = time.perf_counter()
        say("you", command)
        activate("intent")
    try:
        routed = router.route(command)
        with LOCK:
            STATE["tool"] = routed["tool"]
            log_row("jev", f"Routed the request, {len(routed['probabilities'])} questions in one call",
                    routed["latency_ms"], routed["confidence"]["tool"])
            STATE["routing"] = {
                "tool": routed["tool"],
                "priority": routed["priority"],
                "confidence": round(routed["confidence"]["tool"], 3),
                "latency_ms": routed["latency_ms"],
            }
        ask = router.missing_piece(routed)
        if ask:
            # Jev's confidence is calibrated, so a thin sentence is worth one short question
            # rather than a guessed destination and a browser run spent on it.
            with LOCK:
                STATE["phase"] = "waiting"
                STATE["running"] = False
                STATE["elapsed_ms"] = round((time.perf_counter() - STATE["started_at"]) * 1000)
                say("agent", ask)
            return
        run_booking(command, routed)
    except Exception as error:
        fail(error)


def run_booking(command, routed):
    trip = parse_intent(command, need_date=True)
    trip["party"] = routed["party"]
    trip["trip_type"] = routed["trip_type"]
    trip["priority"] = routed["priority"]
    if trip["trip_type"] == "round_trip" and not trip.get("return_pretty"):
        raise ValueError("Tell me the return date too and I will book the round trip.")
    with LOCK:
        STATE["itinerary"] = trip
        mark("intent")
        say(
            "agent",
            f"{trip['origin']} to {trip['destination']}, {trip['pretty']}"
            + (f" returning {trip['return_pretty']}" if trip.get("return_pretty") else "")
            + f", {trip['party']} adult{'s' if trip['party'] > 1 else ''}, economy, "
            + ("one way" if trip["trip_type"] == "one_way" else "round trip")
            + f". Jev routed it in {routed['latency_ms']} ms. Finding the {routed['priority']} fare now.",
        )
        log_row("llm", "Read the places and the date out of the sentence", trip["latency_ms"])
        activate("open")
        STATE["phase"] = "running"
    drive_booking(trip)


def drive_booking(trip):
    global AGENT, FEED
    if FEED:
        FEED.stop()
    ensure_window()
    AGENT = Agent("https://www.google.com/travel/flights?hl=en", build_goal(trip), screenshots=False)
    FEED = Screencast(AGENT.browser.target)
    FEED.start()
    # macOS clamps an offscreen window position back onto the display, so the demo window is
    # minimised instead. Focus emulation keeps it compositing, so the feed is unaffected.
    hide_browser(True)
    with LOCK:
        mark("open")

    known = {x["id"] for x in page_targets()}
    handoff = None
    settled = False
    picked = False
    logged_actions = 0
    logged_text = 0
    while AGENT.state["status"] not in {"done", "blocked"}:
        url = AGENT.state["page"]["url"]
        if "/travel/flights/search" in url and not picked:
            # The winning fare is usually below the fold, where the click guard will not go, and
            # its price is buried in prose. Read the rows and let Jev choose on the numbers.
            chosen = picker.select_fare(AGENT.browser.target, trip.get("priority", "cheapest"))
            if chosen:
                picked = True
                with LOCK:
                    mark("search"), mark("select")
                    STATE["last_action"] = picker.describe(chosen)[:110]
                    STATE["last_probability"] = chosen["confidence"]
                    STATE["fare"] = chosen
                    log_row("jev", f"Chose ${chosen['price']} {chosen['airline']} from "
                                   f"{chosen['considered']} fares", chosen["latency_ms"],
                            chosen["confidence"])
                    say("agent", picker.describe(chosen))
                    STATE["elapsed_ms"] = round((time.perf_counter() - STATE["started_at"]) * 1000)
                time.sleep(0.9)
                try:
                    AGENT.state["page"] = AGENT.browser.observe(screenshot=False)
                except Exception:
                    pass
                continue
        if "/travel/flights/booking" in url and not settled:
            # Google paints the fare list a beat after the booking page opens. Scrolling before
            # then finds only the footer, so hold the loop until the panel is actually there.
            settled = True
            deadline = time.perf_counter() + 9
            while time.perf_counter() < deadline:
                page = AGENT.browser.observe(screenshot=False)
                AGENT.state["page"] = page
                with LOCK:
                    STATE["elapsed_ms"] = round((time.perf_counter() - STATE["started_at"]) * 1000)
                    STATE["last_action"] = "Waiting for fare options to load"
                if any(s in page["text"] for s in ("Booking options", "Basic Economy", "Book with")):
                    break
                if not STATE.get("running"):
                    break
                time.sleep(0.35)
        AGENT.state["goal"] = build_goal(trip, AGENT.state["page"]["url"])
        state = AGENT.command("tick")
        if state["status"] == "done" and "/travel/flights/booking" in state["page"]["url"]:
            # Phase one is satisfied. Reopen the run so phase two can continue to the airline.
            AGENT.state["status"] = "ready"
        with LOCK:
            STATE["elapsed_ms"] = round((time.perf_counter() - STATE["started_at"]) * 1000)
            read_progress(state["page"], trip)
            if state["history"]:
                last = state["history"][-1]
                STATE["last_action"] = last["action"][:110]
                STATE["last_probability"] = last["probability"]
            STATE["stats"] = summarise(state, trip)
            while logged_actions < len(state["history"]):
                entry = state["history"][logged_actions]
                decision = state["decisions"][logged_actions] if logged_actions < len(state["decisions"]) else {}
                log_row("jev", entry["action"], decision.get("latency_ms"), entry.get("probability"))
                logged_actions += 1
            while logged_text < len(state["text_calls"]):
                call = state["text_calls"][logged_text]
                log_row("llm", f"Wrote the value for {call.get('field', 'a field')}",
                        call.get("latency_ms"))
                logged_text += 1
            if not STATE["running"]:
                break
        handoff = airline_handoff(known)
        if handoff:
            break

    final = AGENT.snapshot()
    if handoff:
        # Google hands a booking off by opening the airline in a new tab, sometimes two. Follow it
        # in the tab already being recorded and close the rest, so the whole booking happens in one
        # tab from the first click to the passenger form. The feed never has to be restarted
        # either, which removes a visible stutter at the handover.
        destination = handoff.get("url", "")
        for extra in page_targets():
            if extra["id"] != AGENT.browser.target:
                try:
                    cdp_http(f"/json/close/{extra['id']}")
                except Exception:
                    pass
        try:
            AGENT.browser.call("Page.navigate", url=destination)
        except Exception:
            pass
        # Stop here on purpose. The passenger form is the handover point, and nothing is typed in it.
        time.sleep(1.6)
        try:
            final["page"] = AGENT.browser.observe(screenshot=False)
        except Exception:
            final["page"] = {**final["page"], "url": destination}
        with LOCK:
            mark("search"), mark("select"), mark("details")
            STATE["handoff_url"] = destination
    # The agent stops the moment it arrives; the page finishes painting a beat later.
    deadline = time.perf_counter() + (0 if handoff or STATE.get("stopped") else 6)
    while time.perf_counter() < deadline:
        page = AGENT.browser.observe(screenshot=False)
        with LOCK:
            read_progress(page, trip)
            done = next(s for s in STATE["steps"] if s["id"] == "details")["status"] == "done"
        if done:
            final["page"] = page
            break
        time.sleep(0.3)

    trace = Path.cwd() / "artifacts" / "agent-one" / "latest"
    trace.mkdir(parents=True, exist_ok=True)
    (trace / "state.json").write_text(
        json.dumps(
            {**{k: v for k, v in final.items() if k != "browser"}, "goal_used": build_goal(trip, "")},
            indent=2,
        )
    )
    with LOCK:
        STATE["phase"] = "done"
        STATE["running"] = False
        STATE["agent_status"] = final["status"]
        STATE["elapsed_ms"] = round((time.perf_counter() - STATE["started_at"]) * 1000)
        STATE["stats"] = summarise(final, trip)
        reached = next(s for s in STATE["steps"] if s["id"] == "details")["status"] == "done"
        seconds = STATE["elapsed_ms"] / 1000
        cost = f"{STATE['stats']['actions']} actions, {STATE['stats']['decisions']} Jev decisions, " \
               f"median {STATE['stats']['median_ms']} ms, ${STATE['stats']['cost_usd']:.4f}."
        if reached:
            headline = f"Your seat is held. {seconds:.2f} seconds, {cost}"
        else:
            headline = f"I stopped after {seconds:.2f} seconds without reaching the passenger form. {cost}"
        say("agent", headline + " That is the airline's own passenger form on screen. "
                     "Nothing personal was entered and nothing was paid.")


def hand_over():
    """Stop the agent loop and leave the tab and its live view up, so the person can carry on."""
    with LOCK:
        STATE["running"] = False
        STATE["stopped"] = True


def reset():
    """Loading the page starts a clean demo: stop the agent, drop the transcript, blank the pane.

    A recording is a sequence of takes, and a refresh is how you start the next one. Leaving the
    last run's clock and checkmarks on screen makes a fresh take look like it is already half done.
    """
    global FEED
    stop_all()
    FEED = None
    # Providers drop an idle connection, so a take that starts ten minutes after the last one pays
    # the handshake again. A refresh is how a take begins, so warm them here as well as at startup.
    threading.Thread(target=warm_up, daemon=True).start()
    with LOCK:
        STATE.clear()
        STATE.update(blank_state())


def response_state():
    with LOCK:
        payload = {k: v for k, v in STATE.items() if k != "started_at"}
        if STATE.get("running") and STATE.get("started_at"):
            payload["elapsed_ms"] = round((time.perf_counter() - STATE["started_at"]) * 1000)
        payload["feed"] = bool(FEED and FEED.frame)
        return payload


def stop_all():
    global AGENT, FEED
    with LOCK:
        STATE["running"] = False
    # Every step here is best effort. Teardown runs when something has already gone wrong, and a
    # tab that closed on its own must not stop the next run from starting.
    if FEED:
        try:
            FEED.stop()
        except Exception:
            pass
        FEED = None
    if AGENT:
        try:
            AGENT.close()
        except Exception:
            pass
        finally:
            AGENT = None
    # AGENT.close() only closes the tab it was last pointed at, and an airline opens more tabs than
    # we ever hear about: Google's handoff lands twice, and checkout pages spawn their own. Tracking
    # ids never kept up, so sweep the lot. This profile is a throwaway used by nothing but this app,
    # so every tab in it is ours to close.
    try:
        sweep_stale()
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def send(self, status, content, mime="application/json"):
        content = content if isinstance(content, bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(content)
        except BrokenPipeError:
            pass

    def stream(self):
        """Push frames down one connection as they arrive, so the pane plays rather than flickers."""
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=jevframe")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        deadline = time.monotonic() + 3600
        try:
            while time.monotonic() < deadline:
                data = FEED.frame if FEED else None
                if data and data != last:
                    last = data
                    blob = base64.b64decode(data)
                    self.wfile.write(
                        b"--jevframe\r\nContent-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(blob)}\r\n\r\n".encode()
                    )
                    self.wfile.write(blob)
                    self.wfile.write(b"\r\n")
                else:
                    time.sleep(0.006)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{PORT}":
            return self.send(403, "Forbidden", "text/plain")
        path = urlparse(self.path).path
        if path == "/api/state":
            return self.send(200, json.dumps(response_state()))
        if path == "/api/stream.mjpg":
            return self.stream()
        if path == "/api/frame.jpg":
            data = FEED.frame if FEED else None
            if not data:
                return self.send(204, b"", "image/jpeg")
            return self.send(200, base64.b64decode(data), "image/jpeg")
        files = {
            "/": ("agent.html", "text/html"),
            "/agent.js": ("agent.js", "text/javascript"),
            "/agent.css": ("agent.css", "text/css"),
        }
        if path not in files:
            return self.send(404, "Not found", "text/plain")
        if path == "/":
            reset()
        name, mime = files[path]
        body = (ROOT / "static" / name).read_text().replace("__TOKEN__", TOKEN)
        self.send(200, body, mime + "; charset=utf-8")

    def do_POST(self):
        if (
            self.headers.get("Host") != f"127.0.0.1:{PORT}"
            or self.headers.get("X-Demo-Token") != TOKEN
            or self.headers.get("Origin") not in (None, ORIGIN)
        ):
            return self.send(403, json.dumps({"error": "Local requests only"}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 4096:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length))
            action = urlparse(self.path).path.removeprefix("/api/")
            if action == "window":
                hide_browser(not body.get("visible"))
                return self.send(200, json.dumps({"visible": bool(body.get("visible"))}))
            if action == "stop":
                hand_over()
                return self.send(200, json.dumps(response_state()))
            if action != "run":
                raise ValueError("Unknown action")
            command = str(body.get("command", "")).strip()
            if not 3 < len(command) < 500:
                raise ValueError("Type a sentence between 4 and 500 characters")
            with LOCK:
                if STATE.get("running"):
                    raise ValueError("A booking is already running")
            stop_all()
            threading.Thread(target=run_task, args=(command,), daemon=True).start()
            time.sleep(0.15)
            return self.send(200, json.dumps(response_state()))
        except ValueError as error:
            self.send(400, json.dumps({"error": str(error)}))
        except Exception:
            traceback.print_exc()
            self.send(500, json.dumps({"error": "Local run failed. Reload and try again."}))

    def log_message(self, *_args):
        pass


def warm_up():
    """Pay the TLS and HTTP/2 handshake before the demo rather than during it.

    The first Jev call of a cold process measured 1236 ms against 77 to 242 ms once the connection
    is up, and the first text call 1952 ms. That is a second and a half of the headline number
    spent on a handshake, so spend it at startup on a sentence nobody sees.
    """
    for attempt in (lambda: router.route("warm up"), lambda: parse_intent("warm up", need_date=False)):
        try:
            attempt()
        except Exception:
            pass


def main():
    from .demo import load_environment

    load_environment()
    # Startup must never fail because the browser was not ready yet. A stale tab or a window left
    # on screen is a cosmetic problem; a server that refuses to boot is not.
    try:
        sweep_stale()
    except Exception as error:
        print(f"Browser not ready at startup ({type(error).__name__}); continuing.", flush=True)
    threading.Thread(target=warm_up, daemon=True).start()
    STATE.update(blank_state())
    atexit.register(stop_all)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Jev Flash: {ORIGIN}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_all()
        server.server_close()


if __name__ == "__main__":
    main()
