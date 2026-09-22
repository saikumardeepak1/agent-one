"""Live Google Flights search. Calls TypeSafe; selects a flight but never books or pays."""

import argparse
import base64
import datetime
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jev_ultrafast import Agent

URL = "https://www.google.com/travel/flights?hl=en"

# Google's booking-options step for a chosen flight. Reaching it is the stop line: no payment,
# no personal details, no account. The goal text repeats that constraint for the model.
PICK_PHRASE = {"cheapest": "lowest price", "fastest": "shortest total duration"}

BOOKING_SIGNALS = ("Booking options", "Book with", "Continue to", "Separate tickets")


def build_goal(args, pretty_date):
    goal = (
        f"Find one-way flights from {args.origin} to {args.destination} on {pretty_date}, "
        f"for one adult in economy."
    )
    if args.select:
        goal += (
            f" After the flight results appear, select the flight with the {PICK_PHRASE[args.pick]}."
            " Stop when the booking options for that flight are visible."
            " Never enter personal information or payment details."
        )
    else:
        goal += " Stop when matching flight options are visible. Do not select or book a flight."
    return goal


def verify(page, args, date):
    """Independent checks on the resulting page, not the model's DONE answer."""
    parsed = urlparse(page["url"])
    text = page["text"]
    values = {a["label"].strip(): a.get("value") for a in page["actions"]}
    flights = [a["label"] for a in page["actions"] if "Select flight" in a["label"]]
    short_day, long_day = date.strftime("%a, %b %-d"), date.strftime("%A, %B %-d")
    on_google = parsed.hostname == "www.google.com"

    if args.select:
        # A selected flight leaves the search form behind, so the itinerary summary is the evidence.
        checks = {
            "on_booking_page": on_google and parsed.path == "/travel/flights/booking",
            "origin_in_summary": args.origin_field in text,
            "destination_in_summary": args.destination_field in text,
            "one_way": "One way" in text,
            "date_in_summary": short_day in text,
            "flight_selected": "Selected flights" in text,
            "booking_options_shown": any(s in text for s in BOOKING_SIGNALS),
        }
    else:
        encoded = parse_qs(parsed.query).get("tfs", [""])[0]
        try:
            date_in_url = date.isoformat().encode() in base64.urlsafe_b64decode(
                encoded + "=" * (-len(encoded) % 4)
            )
        except ValueError:
            date_in_url = False
        checks = {
            "on_search_page": on_google and parsed.path == "/travel/flights/search",
            "one_way": values.get("Change ticket type. One way") == "One way",
            "origin": values.get("Where from?") == args.origin_field,
            "destination": values.get("Where to?") == args.destination_field,
            "date": values.get("Departure") == short_day,
            "year": date_in_url or f"departing {date.isoformat()}" in text,
            "results": bool(flights) and all(long_day in f for f in flights),
        }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "final_url": page["url"],
        "itinerary": [line for line in text.split("\n") if line.strip()][:26] if args.select else None,
        "visible_flights": flights[:4],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="Portland, Oregon", help="how the goal names the origin")
    parser.add_argument("--origin-field", default="Portland", help="value Google shows once resolved")
    parser.add_argument("--destination", default="San Francisco")
    parser.add_argument("--destination-field", default="San Francisco")
    parser.add_argument("--date", default="2026-10-15", help="YYYY-MM-DD departure date")
    parser.add_argument("--pick", default="cheapest", choices=["cheapest", "fastest"])
    parser.add_argument("--select", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--output", default="artifacts/flights/latest")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    date = datetime.date.fromisoformat(args.date)
    goal = build_goal(args, date.strftime("%B %-d, %Y"))
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=True)
    print(f"GOAL: {goal}\n", flush=True)

    agent = Agent(URL, goal)
    try:
        for state in agent.run():
            last = state["history"][-1] if state["history"] else {}
            print(state["elapsed_ms"], state["status"], last.get("action", "")[:70], flush=True)
    finally:
        state = agent.snapshot()
        state["goal_used"] = goal
        # The agent's job ends when it reaches the booking page. Google paints the booking-options
        # panel a beat later, so the independent check waits for it rather than asserting mid-paint.
        settle_ms = 0
        if args.select:
            settle_start = time.perf_counter()
            while settle_ms < 8000:
                page = agent.browser.observe(screenshot=False)
                if any(s in page["text"] for s in BOOKING_SIGNALS):
                    break
                time.sleep(0.25)
                settle_ms = round((time.perf_counter() - settle_start) * 1000)
            state["page"] = page
            state["settle_after_done_ms"] = settle_ms
        state["verification"] = verify(state["page"], args, date)
        (folder / "state.json").write_text(json.dumps(state, indent=2))
        if not args.keep_open:
            agent.close()
    v = dict(state["verification"])
    print(f"\nagent time {state['elapsed_ms']} ms"
          + (f" + {state.get('settle_after_done_ms', 0)} ms waiting for the booking panel to paint"
             if args.select else ""))
    print(json.dumps(v, indent=2))
    if not v["passed"]:
        raise SystemExit("Final page did not satisfy the checks")


if __name__ == "__main__":
    main()
