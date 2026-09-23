"""The same agent, deciding with a text model instead of Jev.

This exists to be measured against `model.choose`, so it is deliberately not a strawman. It gets
the identical element table, the identical operation list, and the identical rules Jev is given.
The only difference is what answers the question: a text model writing JSON, against a decision
model picking from a fixed set.

That difference is the whole experiment. A text model can name an element that is not on the page,
or return JSON that does not parse, so a real harness has to validate and ask again. Those retries
are not a flaw in this file; they are the thing being measured, and they are counted rather than
hidden. Jev has no equivalent, because there is no invalid answer for it to give.
"""

import json
import os
import time

from . import model
from .questions import NEXT_ACTION, TARGET

# How many times a real harness would re-ask before giving up on a step.
ATTEMPTS = int(os.environ.get("BASELINE_ATTEMPTS", "4"))


class NoUsableAction(ValueError):
    """Every attempt was rejected. Carries why, so the comparison can report it rather than
    reducing a run to the word "failed"."""

    def __init__(self, rejected):
        self.rejected = rejected
        super().__init__(f"Text model gave no usable action in {len(rejected)} attempts: "
                         + "; ".join(rejected))

# The rules go in the system prompt, not into the JSON payload as data. A text model weights its
# system message far more heavily than a field buried in a user object, and burying them there was
# handicapping the baseline: it chose BLOCKED on the second step of every run. Jev receives the
# same two rule sets through its instructions, so this is what makes the comparison fair.
INSTRUCTIONS = f"""You are choosing the single next browser action for an agent.

{NEXT_ACTION}

{TARGET}

Reply with JSON only, in exactly this form:
{{"operation": "<one of the offered operations>", "target": "<one of the offered targets>"}}

The operation must be copied exactly from a key of the "operations" object you are given.
If the chosen operation appears in "targets", the target must be copied exactly from that
operation's keys. If it does not appear there, use an empty string for target.
Do not invent an operation or a target that is not listed.

DONE and BLOCKED end the run, so use them only when they genuinely apply. There is almost always
a useful control on the page; prefer it."""


def payload(state, goal, history, elements, targets, operations):
    """Everything Jev is given, in the shape a text model can read."""
    return {
        "goal": goal,
        "page": {k: state[k] for k in ("url", "title", "text")},
        "elements": elements,
        "operations": operations,
        "targets": {
            operation: {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            }
            for operation, candidates in targets.items()
        },
        "recent_actions": [
            {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
        ],
    }


def ask(body):
    key = os.environ["TEXT_MODEL_API_KEY"]
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    return model.post_json(base + "/chat/completions", key, body)


def choose(state, goal, history):
    """Same signature and same return shape as model.choose, so the agent loop cannot tell them
    apart. Adds `attempts` and `rejected`, which are the numbers this file exists to produce."""
    elements, targets, controls = model.action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")

    body = {
        "model": os.environ.get("TEXT_MODEL", "openai/gpt-oss-20b"),
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
        **model.reasoning_options(os.environ.get("TEXT_MODEL_BASE_URL", "https://api.groq.com/openai/v1")),
        "messages": [
            {"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": json.dumps(
                payload(state, goal, history, elements, targets, operations))},
        ],
    }

    started, throttled = time.perf_counter(), model.THROTTLE_SECONDS
    rejected, usage, operation, target = [], {}, None, None
    for attempt in range(ATTEMPTS):
        try:
            result = ask(body)
        except RuntimeError as error:
            # The provider rejected its own generation. A real harness retries; it still costs
            # wall clock, so it is recorded rather than hidden.
            rejected.append(f"provider: {str(error)[:80]}")
            continue
        usage = result.get("usage", usage)
        try:
            answer = json.loads(result["choices"][0]["message"]["content"])
            operation = answer["operation"]
            target = answer.get("target") or None
        except (ValueError, KeyError, TypeError):
            rejected.append("unparseable JSON")
            operation = target = None
            continue
        if operation not in operations:
            rejected.append(f"operation not offered: {str(operation)[:40]}")
            operation = target = None
            continue
        if operation in targets:
            if target not in targets[operation]:
                rejected.append(f"target not on the page: {str(target)[:40]}")
                operation = target = None
                continue
        else:
            target = None
        break

    # Subtract time spent asleep on a rate limit. That is the account tier, not the model.
    elapsed = time.perf_counter() - started - (model.THROTTLE_SECONDS - throttled)
    latency = round(elapsed * 1000)
    if operation is None:
        raise NoUsableAction(rejected)

    if operation in targets:
        choice = targets[operation][target]["id"]
        probabilities = {targets[operation][target]["id"]: 1.0}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities = {choice: 1.0}

    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        # A text model reports no calibrated probability. Saying 1.0 would be a claim it never
        # made, so the confidence fields are empty and the comparison does not pretend otherwise.
        "confidence": None,
        "probabilities": probabilities,
        "operation_probabilities": {},
        "target_probabilities": {},
        "target_confidence": None,
        "raw_answers": {"operation": operation, "target": target},
        "model": body["model"],
        "usage": usage,
        "latency_ms": latency,
        "attempts": len(rejected) + 1,
        "rejected": rejected,
        "request": body,
    }
