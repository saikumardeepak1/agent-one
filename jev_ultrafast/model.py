"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values."""

import json
import math
import os
import time

import httpx

from .questions import NEXT_ACTION, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)
SYSTEM_ONE = "https://api.typesafe.ai/v1/systemone"


# A 400 normally means the request itself is wrong and retrying is pointless. These two codes are
# the exception: the request was accepted and the model's own output failed validation.
GENERATION_FAULTS = ("json_validate_failed", "output_parse_failed")


def _retryable_generation(response):
    try:
        return response.json().get("error", {}).get("code") in GENERATION_FAULTS
    except ValueError:
        return False


# Seconds spent sleeping on provider throttling since the last reset. A rate limit is a property
# of the account tier, not of the approach being measured, so the comparison subtracts it rather
# than reporting a free-tier queue as if it were decision latency.
THROTTLE_SECONDS = 0.0


def reset_throttle():
    global THROTTLE_SECONDS
    spent, THROTTLE_SECONDS = THROTTLE_SECONDS, 0.0
    return spent


def _throttled_sleep(seconds):
    global THROTTLE_SECONDS
    THROTTLE_SECONDS += seconds
    time.sleep(seconds)


def retry_after(response, attempt):
    """Honour the provider's own Retry-After when it sends one, and back off otherwise."""
    header = response.headers.get("retry-after") or response.headers.get("x-ratelimit-reset-tokens")
    if header:
        try:
            return min(float(str(header).rstrip("s")), 30.0)
        except ValueError:
            pass
    return 0.5 * 2**attempt


def post_json(url, key, body, attempts=5):
    for attempt in range(attempts):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            # A pooled connection the provider has already closed fails on first use, which is a
            # dead socket rather than a dead service. Retrying gets a new one; giving up here cost
            # a whole demo run.
            if attempt < attempts - 1:
                time.sleep(0.25 * 2**attempt)
                continue
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < attempts - 1:
            _throttled_sleep(retry_after(response, attempt))
            continue
        if response.status_code == 400 and attempt < attempts - 1 and _retryable_generation(response):
            # The provider produced output that failed its own JSON check. That is a sampling
            # accident, not a bad request: the identical body succeeds on the next try. Measured
            # 2 failures in 4 calls before the reasoning budget was capped, so the retry stays
            # as a backstop even now that the cause is fixed.
            time.sleep(0.2 * 2**attempt)
            continue
        if response.is_error:
            detail = response.text[:220].replace("\n", " ")
            raise RuntimeError(
                f"Model provider returned HTTP {response.status_code}: {detail}; no action executed."
            )
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            # Probabilities come back rounded, so on a wide flat distribution the reported
            # argmax can differ from the server's full-precision pick by a rounding step.
            # Same 0.02 tolerance the sum check above already uses.
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 0.02
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid TypeSafe response ({type(error).__name__}); no action executed.") from None
    if not valid:
        chosen, keys, total = answer.get("choice"), set(probabilities), sum(probabilities.values())
        if chosen not in ids:
            why = "choice not offered"
        elif keys != set(ids):
            why = f"option set differs by {sorted(keys ^ set(ids))[:4]}"
        elif abs(total - 1) >= 0.02:
            why = f"probabilities sum to {total:.3f}"
        else:
            why = "chosen option is not the argmax"
        raise ValueError(f"Invalid TypeSafe response: {why}; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def choose(state, goal, history):
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started, throttled = time.perf_counter(), THROTTLE_SECONDS
    result = post_json(SYSTEM_ONE, os.environ["TYPESAFE_API_KEY"], body)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def reasoning_options(base):
    """How to tell this provider not to think for long, in the spelling it accepts.

    Every provider names this differently and rejects the others outright, so it is decided in one
    place. Groq uses a flat reasoning_effort and 400s on a nested reasoning object; DeepSeek uses
    thinking; the OpenAI-compatible default is a nested reasoning object. TEXT_MODEL_REASONING
    overrides all of it, with "omit" sending no key at all.
    """
    setting = os.environ.get("TEXT_MODEL_REASONING")
    if setting == "omit":
        return {}
    if setting == "none":
        return {"reasoning": {"enabled": False}}
    if "api.deepseek.com/" in base:
        return {"thinking": {"type": "disabled"}}
    if "api.groq.com" in base:
        # gpt-oss reasons before it answers, and Groq's default effort leaves that unbounded:
        # measured 103 to 291 completion tokens for the same one-word field. On a full page
        # payload it overran max_tokens, the JSON arrived truncated, and Groq rejected the call
        # as json_validate_failed. Low effort answers the same question in under 40 tokens.
        return {"reasoning_effort": "low"}
    return {"reasoning": {"effort": "low"}}


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def field_text(context):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = reasoning_options(base)
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            # Headroom for a reasoning spike. The answer is a dozen tokens; the budget exists so a
            # long think cannot truncate it into invalid JSON.
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
    )
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    return value, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
