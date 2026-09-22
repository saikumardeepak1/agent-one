"""Choosing which flight to take, as a question Jev can actually answer.

The generic loop asks Jev to pick one element out of everything on the page, with the price written
into prose inside an aria-label, and it rejects any element whose centre is outside the viewport.
On Google Flights the cheapest fare is usually under "Other flights", a thousand pixels down, so
the honest answer to "which of these can I click" excludes the very row that wins. That is not a
prompt problem and no wording fixes it.

So the harness reads the result rows itself, turns them into numbers, and hands Jev a short choice
over structured options. Jev still decides. It is simply being asked something a decision model can
be right about: twelve options with a price, a duration and a stop count, against a stated
priority. The code does the scrolling and the clicking, which was never the model's job.
"""

import json
import os
import re
import time

from . import model
from .tab import Tab

# Every result row is an <li> holding a role=link whose label opens with the fare.
ROWS = """[...document.querySelectorAll('li')]
    .map(li => li.querySelector('[role="link"][aria-label]'))
    .filter(e => e && /^From [\\d,]+ US dollars/.test(e.getAttribute('aria-label')))
    .filter((e, i, all) => all.indexOf(e) === i)"""

READ = (
    """(() => { const rows = """
    + ROWS
    + """;
  const parsed = rows.map((e, i) => {
    const aria = e.getAttribute('aria-label');
    const text = ((e.closest('li') || e).innerText || '').replace(/\\s+/g, ' ');
    const price = aria.match(/^From ([\\d,]+) US dollars/);
    const stops = aria.match(/(Nonstop|\\d+ stop)/i);
    const airline = aria.match(/flight with ([^.]+?)\\./);
    const leaves = aria.match(/Leaves .*? at (\\d{1,2}:\\d{2}\\s*[AP]M)/);
    const duration = text.match(/(\\d+ hr(?: \\d+ min)?|\\d+ min)/);
    return {
      index: i,
      price: price ? Number(price[1].replace(/,/g, '')) : null,
      stops: stops ? stops[1] : '',
      airline: airline ? airline[1].slice(0, 40) : '',
      departs: leaves ? leaves[1] : '',
      duration: duration ? duration[1] : ''
    };
  });
  const priced = parsed.filter(r => r.price);
  return priced.length ? JSON.stringify(parsed) : null;
})()"""
)

RULES = """Choose the one flight that best serves the traveller's stated priority.
cheapest means the lowest price_usd. fastest means the lowest total_minutes.
balanced means a good price without a badly long journey.
Judge only the numbers given. Do not prefer an option for being listed first."""


def minutes(text):
    hours = re.search(r"(\d+)\s*hr", text or "")
    mins = re.search(r"(\d+)\s*min", text or "")
    total = int(hours.group(1)) * 60 if hours else 0
    return total + (int(mins.group(1)) if mins else 0)


def read_rows(tab, limit=12):
    raw = tab.settle(READ, timeout=12)
    rows = [row for row in json.loads(raw) if row.get("price")] if raw else []
    for row in rows:
        row["total_minutes"] = minutes(row.pop("duration", ""))
        row["stop_count"] = 0 if row["stops"].lower().startswith("nonstop") else minutes(row["stops"]) or 1
    return rows[:limit]


def decide(rows, priority):
    """One Jev Choice over the fares, with the numbers as numbers."""
    criteria = {
        str(row["index"]): {
            "price_usd": row["price"],
            "total_minutes": row["total_minutes"],
            "stops": row["stops"] or "unknown",
            "airline": row["airline"],
            "departs": row["departs"],
        }
        for row in rows
    }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {"priority": priority, "fares": criteria},
        "questions": {
            "fare": {"type": "choice", "criteria": criteria,
                     "instructions": {"rules": RULES, "priority": priority}}
        },
    }
    started = time.perf_counter()
    result = model.post_json(model.SYSTEM_ONE, os.environ["TYPESAFE_API_KEY"], body)
    answer = model.validate_choice(result["answers"].get("fare", {}), criteria)
    chosen = next(row for row in rows if str(row["index"]) == answer["choice"])
    return {
        **chosen,
        "confidence": answer["confidence"],
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "considered": len(rows),
    }


def select_fare(target_id, priority):
    """Read the fares, let Jev choose one, scroll to it and click it. None when there is nothing yet."""
    with Tab(adopt=target_id) as tab:
        rows = read_rows(tab)
        if len(rows) < 2:
            return None
        chosen = decide(rows, priority)
        # The winning row is usually well below the fold, so bring it into view before measuring.
        point = tab.evaluate(
            "(() => { const e = " + ROWS + f"[{chosen['index']}];"
            " if (!e) return null;"
            " e.scrollIntoView({block: 'center', behavior: 'instant'});"
            " return 1; })()"
        )
        if not point:
            return None
        time.sleep(0.35)
        spot = tab.evaluate(
            "(() => { const e = " + ROWS + f"[{chosen['index']}];"
            " if (!e) return null; const r = e.getBoundingClientRect();"
            " if (r.bottom < 0 || r.top > innerHeight) return null;"
            " return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)}; })()"
        )
        if not spot:
            return None
        # Move first: these rows are hover-gated and ignore a press that arrives cold.
        tab.call("Input.dispatchMouseEvent", type="mouseMoved", **spot)
        for event in ("mousePressed", "mouseReleased"):
            tab.call("Input.dispatchMouseEvent", type=event, button="left", clickCount=1, **spot)
        return chosen


def describe(chosen):
    stops = chosen["stops"].lower() if chosen["stops"] else "unknown stops"
    hours, mins = divmod(chosen["total_minutes"], 60)
    span = f"{hours}h {mins:02d}m" if hours else f"{mins}m"
    return (f"${chosen['price']} {chosen['airline']} {chosen['departs']}, {span}, {stops}. "
            f"Jev picked it out of {chosen['considered']} fares in {chosen['latency_ms']} ms.")
