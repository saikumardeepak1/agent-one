"""Run the same booking twice, once decided by Jev and once by a text model, and measure both.

Everything except the decision maker is shared: the same browser driver, the same element table,
the same executor, the same goal, the same start page. The swap is one environment variable, so a
difference in the numbers has one cause.

Nothing here decides in advance which side wins. It prints what happened, including the runs that
fail, because a comparison that can only produce one answer is not worth publishing.

    uv run --env-file .env python scripts/compare.py --runs 3
"""

import argparse
import json
import os
import statistics
import time
from pathlib import Path

from jev_ultrafast import model

URL = "https://www.google.com/travel/flights?hl=en"

GOAL = (
    "Find one-way flights from {origin} to {destination} on {date}, for one adult in economy. "
    "After the flight results appear, select the flight with the lowest price. "
    "Stop when the booking options for that flight are visible. "
    "Never enter personal information or payment details."
)


def run_once(decider, goal, budget):
    """One booking, start to finish, with the chosen decision maker."""
    os.environ["AGENT_ONE_DECIDER"] = decider
    from jev_ultrafast import Agent  # imported after the variable is set

    decisions, error, failed = [], None, []
    model.reset_throttle()
    started = time.perf_counter()
    agent = Agent(URL, goal)
    try:
        while True:
            state = agent.command("tick")
            if state["status"] in {"done", "blocked"} or not state.get("running", True):
                error = "gave up: chose BLOCKED" if state["status"] == "blocked" else error
                break
            if time.perf_counter() - started > budget:
                error = f"budget of {budget}s exhausted"
                break
            if len(state["decisions"]) >= 40:
                error = "step budget exhausted"
                break
    except Exception as failure:
        error = f"{type(failure).__name__}: {str(failure)[:120]}"
        failed = list(getattr(failure, "rejected", []))
    finally:
        state = agent.snapshot()
        decisions = state.get("decisions", [])
        try:
            agent.close()
        except Exception:
            pass

    throttle_ms = round(model.reset_throttle() * 1000)
    latencies = [d["latency_ms"] for d in decisions if d.get("latency_ms")]
    attempts = [d.get("attempts", 1) for d in decisions]
    # Tokens per decision is the other half of the story: a text model has to be handed the whole
    # element table as prose every step, where Jev is handed structured criteria.
    # TypeSafe reports input_tokens, OpenAI-compatible providers report prompt_tokens. Reading
    # only one of them made Jev look like it used no tokens at all, which is not true.
    prompt_tokens = [
        d.get("usage", {}).get("prompt_tokens") or d.get("usage", {}).get("input_tokens") or 0
        for d in decisions
    ]
    rejected = [r for d in decisions for r in d.get("rejected", [])] + failed
    wall_ms = round((time.perf_counter() - started) * 1000)
    return {
        "decider": decider,
        "wall_ms": wall_ms,
        # A free-tier rate limit is a property of the account, not of the approach, so it is
        # reported separately instead of being counted as thinking time.
        "throttle_ms": throttle_ms,
        "wall_excluding_throttle_ms": wall_ms - throttle_ms,
        "decisions": len(decisions),
        "median_ms": round(statistics.median(latencies)) if latencies else None,
        "total_decision_ms": sum(latencies),
        "median_prompt_tokens": round(statistics.median(prompt_tokens)) if prompt_tokens else None,
        "extra_attempts": sum(attempts) - len(attempts),
        "rejected": rejected,
        # The actual decision trace, so a run that ends early can be read rather than guessed at.
        "operations": [d.get("operation") for d in decisions],
        "reached_booking": "/travel/flights/booking" in state.get("page", {}).get("url", ""),
        "error": error,
    }


def summarise(rows):
    """Report only what the runs support.

    Wall clock is deliberately not compared across a completed run and a failed one. A run that
    gave up at step four has no meaningful duration, and putting its number beside a finished run
    would be measuring failure and calling it speed. Wall clock is therefore summarised over
    completed runs only, and says so when a side has none.
    """
    out = {}
    for decider in ("jev", "llm"):
        runs = [r for r in rows if r["decider"] == decider]
        if not runs:
            continue
        finished = [r for r in runs if r["reached_booking"]]
        # Per-decision latency is comparable whether or not the run finished, because it measures
        # one question and one answer. Throttling is already excluded from it.
        latencies = [r["median_ms"] for r in runs if r["median_ms"]]
        out[decider] = {
            "runs": len(runs),
            "completed": len(finished),
            "median_decision_ms": round(statistics.median(latencies)) if latencies else None,
            "median_prompt_tokens": round(statistics.median(
                [r["median_prompt_tokens"] for r in runs if r["median_prompt_tokens"]] or [0])),
            "median_wall_s_completed_only": (
                round(statistics.median([r["wall_excluding_throttle_ms"] for r in finished]) / 1000, 2)
                if finished else "no completed runs"
            ),
            "median_throttle_s": round(statistics.median([r["throttle_ms"] for r in runs]) / 1000, 2),
            "total_extra_attempts": sum(r["extra_attempts"] for r in runs),
            "distinct_rejections": sorted({r.split(":")[0] for row in runs for r in row["rejected"]}),
        }

    jev, llm = out.get("jev", {}), out.get("llm", {})
    if jev and llm:
        if not llm["completed"] or not jev["completed"]:
            out["verdict"] = (
                "Not a speed result. One side did not complete the task, so the durations are not "
                "comparable. What the runs support is a completion rate and a per-decision latency."
            )
        else:
            out["verdict"] = "Both sides completed; wall clock over completed runs is comparable."
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3, help="runs per decider")
    parser.add_argument("--origin", default="Portland")
    parser.add_argument("--destination", default="Seattle")
    parser.add_argument("--date", default="October 14, 2026")
    parser.add_argument("--budget", type=float, default=180, help="seconds before a run is abandoned")
    parser.add_argument("--out", default="docs/comparison.json")
    args = parser.parse_args()

    goal = GOAL.format(origin=args.origin, destination=args.destination, date=args.date)
    rows = []
    for index in range(args.runs):
        for decider in ("jev", "llm"):
            print(f"\n--- run {index + 1}/{args.runs}, decided by {decider} ---", flush=True)
            row = run_once(decider, goal, args.budget)
            rows.append(row)
            print(
                f"  completed {'yes' if row['reached_booking'] else 'NO '} | "
                f"{row['decisions']} decisions | median decision {row['median_ms']} ms | "
                f"net {row['wall_excluding_throttle_ms'] / 1000:.2f}s "
                f"(+{row['throttle_ms'] / 1000:.0f}s rate limited) | re-asks {row['extra_attempts']}"
                + (f" | {row['error']}" if row["error"] else ""),
                flush=True,
            )
            for rejection in row["rejected"]:
                print(f"    rejected: {rejection}", flush=True)

    report = {"task": goal, "runs": rows, "summary": summarise(rows)}
    Path(args.out).write_text(json.dumps(report, indent=2))
    print("\n" + json.dumps(report["summary"], indent=2))
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
