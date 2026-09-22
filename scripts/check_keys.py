"""Minimal live check that both model credentials work. Costs a fraction of a cent."""

import os

from jev_ultrafast import model


def check_jev():
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {"ticket": "My card was charged twice and nobody has replied."},
        "questions": {
            "category": {
                "type": "choice",
                "criteria": {"billing": "Payment or charge issues", "other": "Anything else"},
                "instructions": "What is this ticket about?",
            }
        },
    }
    result = model.post_json("https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body)
    answer = result["answers"]["category"]
    return f"model={result.get('model')} choice={answer['choice']} confidence={answer['confidence']:.3f}"


def check_text():
    value, meta = model.field_text(
        {
            "goal": "Find one-way flights from Portland, Oregon to San Francisco.",
            "field": {"label": "Where from?", "role": "combobox", "value": ""},
            "page": {"title": "Google Flights", "text": "Where from? Where to? Departure"},
            "recent_actions": [],
        }
    )
    return f"model={meta['model']} latency={meta['latency_ms']}ms value={value!r}"


failed = False
for name, key, check in (
    ("TYPESAFE_API_KEY", "TYPESAFE_API_KEY", check_jev),
    ("TEXT_MODEL_API_KEY", "TEXT_MODEL_API_KEY", check_text),
):
    if not os.environ.get(key):
        print(f"[MISSING] {name} is not set in .env")
        failed = True
        continue
    try:
        print(f"[ok]      {name}  {check()}")
    except Exception as error:
        print(f"[FAIL]    {name}  {type(error).__name__}: {error}")
        failed = True

raise SystemExit(1 if failed else 0)
