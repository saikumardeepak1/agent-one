"""Compare ways of getting between two places, read off Google Maps and ranked by Jev.

Maps puts every travel mode's best duration in one bar on a single page load, so this reads all of
them at once rather than clicking through the tabs. Each button carries a `data-tooltip` naming the
mode and an `aria-checked` marking the active one, which is a far steadier handle than icon order.

Jev does the ranking. Scoring four options against a stated priority is a small ordered judgement
made many times, which is cheap and calibrated here and would be a paragraph of invented reasoning
from a text model.
"""

import json
import os
import time
import urllib.parse

from . import model
from .tab import Tab

MODES = ("Driving", "Transit", "Flights", "Cycling", "Walking")

READ_BAR = """(() => {
  // Maps draws each mode icon as a private-use glyph inside the button text; strip it.
  const clean = (t) => (t || '').replace(/[\\uE000-\\uF8FF]/g, '').replace(/\\s+/g, ' ').trim();
  const bar = [...document.querySelectorAll('[role="radio"][data-tooltip]')]
    .filter(e => ['Driving','Transit','Walking','Cycling','Flights'].includes(e.dataset.tooltip))
    .map(e => ({mode: e.dataset.tooltip,
                duration: clean(e.innerText),
                selected: e.getAttribute('aria-checked') === 'true'}));
  if (!bar.length) return null;
  // The chosen route prints its time, distance and road on three consecutive lines.
  const leg = (document.body.innerText || '')
    .match(/(\\d+ hr \\d+ min|\\d+ hr|\\d+ min)\\s*\\n\\s*([\\d,.]+ (?:miles|mi|km))\\s*\\n\\s*via ([^\\n]{2,40})/);
  return {bar, distance: leg ? leg[2] : '', via: leg ? leg[3].trim() : ''};
})()"""


SCORING = """Score how well this travel option serves the traveller, given their stated priority.
Judge only from the numbers given. An option with no duration is not thereby fast.
A very long duration is a poor option however cheap it is."""

BANDS = [
    "A poor fit: far slower or more costly than the alternatives here.",
    "Workable, but clearly worse than the best option here.",
    "A reasonable choice.",
    "A strong choice on the traveller's stated priority.",
    "Clearly the best option here on the traveller's stated priority.",
]


def maps_url(trip):
    query = urllib.parse.urlencode(
        {"api": 1, "origin": trip["origin"], "destination": trip["destination"],
         "travelmode": "driving", "hl": "en"}
    )
    return f"https://www.google.com/maps/dir/?{query}"


def read_modes(target_id):
    """Read every mode's duration off the Maps panel already open in `target_id`."""
    with Tab(adopt=target_id) as tab:
        found = tab.settle(READ_BAR, timeout=16)
    if not found:
        raise ValueError("Google Maps did not return a route for that pair of places.")
    options = []
    for entry in found["bar"]:
        if not entry["duration"] and entry["mode"] != "Flights":
            continue  # a mode Maps could not route at all
        options.append(
            {
                "mode": entry["mode"],
                "duration": entry["duration"] or "check fares",
                "detail": f"{found['distance']} via {found['via']}"
                if entry["selected"] and found.get("via")
                else "",
            }
        )
    order = {mode: index for index, mode in enumerate(MODES)}
    options.sort(key=lambda option: order.get(option["mode"], 99))
    return options


def rank(options, trip, priority):
    """One Jev request, one Score head per option. Returns the options with a rating attached."""
    if not options:
        return options, 0
    questions = {
        f"option_{index}": {
            "type": "score",
            "criteria": BANDS,
            "instructions": {"rules": SCORING, "priority": priority, "option": option},
        }
        for index, option in enumerate(options)
    }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "journey": f"{trip['origin']} to {trip['destination']}",
            "priority": priority,
            "options": options,
        },
        "questions": questions,
    }
    started = time.perf_counter()
    result = model.post_json(model.SYSTEM_ONE, os.environ["TYPESAFE_API_KEY"], body)
    for index, option in enumerate(options):
        answer = result["answers"].get(f"option_{index}", {})
        raw = answer.get("score", answer.get("choice"))
        try:
            option["score"] = round(float(raw), 2)
        except (TypeError, ValueError):
            option["score"] = None
        option["confidence"] = answer.get("confidence")
    ranked = sorted(options, key=lambda o: (o["score"] is None, -(o["score"] or 0)))
    return ranked, round((time.perf_counter() - started) * 1000)


def describe(options, trip, priority):
    best = options[0]["mode"].lower() if options else "nothing"
    word = {"cheapest": "cheapest", "fastest": "fastest", "balanced": "best overall"}[priority]
    return (f"{len(options)} ways to get from {trip['origin']} to {trip['destination']}. "
            f"Jev rates {best} the {word}. Pick one and I will take it from there.")
