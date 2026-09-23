# Agent One

**A browser agent that books a flight from one sentence, with [TypeSafe's Jev](https://docs.typesafe.ai/introduction) making every bounded decision.**

You type `book the cheapest flight from Portland to Denver on March 3`. Agent One routes the intent, opens Google Flights in a real browser, reads the fare table, picks the cheapest option out of twelve, and stops on the airline's passenger-details page. Measured end to end: **12.88 seconds**, of which **4.02 seconds** was the agent thinking and **8.86 seconds** was waiting for Google and Frontier to paint.

> [!NOTE]
> A recording of an Agent One run is not in this repo yet. The `docs/demo.*` files belong to
> upstream and show a different project, so they are deliberately not shown here.


---

## Why this exists

Most browser agents are slow for one reason: the model talks to itself. It proposes an action, the harness validates it, the action was malformed or referred to an element that isn't there, so it asks again. That loop is where the seconds go, and no amount of prompt tuning removes it, because the model is being asked to be right about something it cannot be reliably right about.

Agent One splits the work by what each layer can actually be correct about.

```
  "book the cheapest flight from Portland to Denver on March 3"
            │
   ┌────────▼─────────┐
   │  JEV  (decide)   │   bounded choices over a fixed option set
   │                  │   6 heads, 1 request, ~150 ms warm:
   │                  │   tool · priority · readiness · date · party · trip_type
   └────────┬─────────┘
            │  routed intent + calibrated confidence
   ┌────────▼─────────┐
   │  LLM  (write)    │   the one thing Jev cannot do:
   │                  │   "Portland" → PDX, "March 3" → 2026-03-03
   └────────┬─────────┘
            │  concrete strings
   ┌────────▼─────────┐
   │  CODE  (act)     │   CDP: navigate, type, scroll, click
   │                  │   never a model's job
   └────────┬─────────┘
            │  12 fare rows, read as numbers
   ┌────────▼─────────┐
   │  JEV  (decide)   │   one Choice over structured fares
   └──────────────────┘   → $132 Alaska, 1 stop
```

Jev gets closed-set decisions where the option list is known and being wrong is expensive. The small text model gets open-ended string production, which is all it is for. Deterministic code does the scrolling and clicking. Nothing retries on malformed JSON, because nothing is asked to produce JSON it could get wrong.

### The fan-out is free

Routing asks six questions in one request:

```python
QUESTIONS = (
    ("tool",      TOOLS),       # book_flight | unclear
    ("priority",  PRIORITY),    # cheapest | fastest | balanced
    ("readiness", READINESS),   # ready | needs_origin | needs_destination | needs_both
    ("date",      DATE),        # given | absent
    ("party",     PARTY),       # 1..6
    ("trip_type", TRIP_TYPE),   # one_way | round_trip
)
```

Six questions cost what one costs, because the bill is for the state and the state is one sentence. A conventional harness makes this six calls, or one call with a large JSON schema and a parse step that can fail.

### Confidence is a control signal, not decoration

Jev returns calibrated confidence. When `readiness` comes back low, Agent One asks *"Which city are you starting from?"* instead of guessing Portland because Portland appears often in training data. See [`missing_piece()`](jev_ultrafast/router.py).

### Picking the cheapest fare is a shaped question, not a prompt trick

The naive approach hands the model the whole page and asks it to click the best flight. That fails on Google Flights, because the cheapest fare usually sits under *Other flights*, roughly a thousand pixels below the fold, and an honest answer to "which of these can I click" excludes the row that wins.

So [`picker.py`](jev_ultrafast/picker.py) reads the result rows itself, turns them into numbers, and hands Jev a short choice over structured options:

```json
{"price_usd": 132, "total_minutes": 224, "stops": "1 stop", "airline": "Alaska", "departs": "6:00 AM"}
```

Jev still decides. It is simply being asked something a decision model can be right about. The code does the scrolling and the clicking, which was never the model's job.

---

## Measured

Two live runs, wall clock, nothing trimmed:

| Run | Result | Time | Steps |
|---|---|---|---|
| PDX → SEA, 1 adult, one way | Jev picked $132 from 12 fares | **11.59 s** | 9/9 |
| PDX → DEN, 2 adults, one way | Stopped on Frontier passenger info, $268 | **12.88 s** | 9/9 |

The log drawer in the UI breaks down every run:

```
16 JEV CALLS 2.23S  |  3 MODEL CALLS 1.79S  |  WAITING ON THE WEB 8.86S  |  TOTAL 12.88S
```

That last column is the honest one. Wall clock swings between 11.6 s and 19.4 s depending entirely on how fast Google and the airline respond. The part Agent One controls is the 4 seconds of decisions, and warm those run at 133 to 200 ms for routing and 400 to 500 ms for the text model.

---

## Running it

You need macOS, [Brave](https://brave.com/), [uv](https://docs.astral.sh/uv/), a TypeSafe API key (currently early access), and any OpenAI-compatible text model key.

```bash
git clone https://github.com/saikumardeepak1/agent-one.git
cd agent-one
uv sync
cp .env.example .env     # fill in the two keys, see below
./run-agent.sh
```

Open **http://127.0.0.1:8767** and type a sentence. Stop everything with `./stop.sh`.

### Keys

Both keys live in `.env`, which is gitignored and never committed:

```bash
TYPESAFE_API_KEY=your_typesafe_key
TYPESAFE_MODEL=jev-latest

TEXT_MODEL_API_KEY=your_groq_or_openrouter_key
TEXT_MODEL_BASE_URL=https://api.groq.com/openai/v1
TEXT_MODEL=openai/gpt-oss-20b
TEXT_MODEL_REASONING=omit
```

`TEXT_MODEL_REASONING=omit` matters for Groq, which returns a 400 on unknown request fields.

### About the browser

`run-agent.sh` starts Brave on a profile of its own at `~/Library/Application Support/AgentOneBrowser`, with remote debugging on port 9222. Your everyday profile is never touched and never has remote debugging enabled.

That separation is not a preference, it is required. Brave refuses to start remote debugging on the default profile at all:

```
DevTools remote debugging requires a non-default data directory.
Specify this using --user-data-dir.
```

The reason is that port 9222 has no authentication. Anything running on the machine that can reach it can drive the browser and read whatever that profile is signed into, which is why infostealer malware looks for it. On a throwaway profile there is nothing to take.

**Sign in to that window once** and it keeps your session for every later run, so the agent gets as far as a logged-in traveller would. Click **OPEN THE BROWSER** in the UI to bring it up.

One macOS quirk: while the demo is running, clicking Brave in the Dock may surface the agent's profile, because macOS hands the Dock icon to whichever Brave started first. Your real profile is untouched. `./stop.sh` gives it back.

---

## Layout

| File | What it does |
|---|---|
| [`router.py`](jev_ultrafast/router.py) | Jev routes the sentence. Six heads, one request. |
| [`picker.py`](jev_ultrafast/picker.py) | Reads fare rows as numbers, lets Jev choose one, clicks it. |
| [`harness.py`](jev_ultrafast/harness.py) | The app server, state machine, and step timeline. |
| [`browser.py`](jev_ultrafast/browser.py) | CDP driving: navigate, type, click. |
| [`tab.py`](jev_ultrafast/tab.py) | Short-lived CDP sessions that never queue behind the agent. |
| [`screencast.py`](jev_ultrafast/screencast.py) | MJPEG frames from the driven tab, read-only. |
| [`model.py`](jev_ultrafast/model.py) | The TypeSafe and text-model transports, with retry. |

```bash
uv run pytest        # 31 tests
```

---

## Known limits

- **It is one tool.** `TOOLS` holds `book_flight` and `unclear`. The routing layer is shaped for more; right now only one is loaded.
- **The fragile part is the selectors, not the models.** `picker.py` parses `From 132 US dollars. 1 stop flight with Alaska...` out of an aria-label. If Google changes that string, fare selection stops working. This is the opposite of what people usually assume breaks.
- **It stops at passenger details on purpose.** Agent One holds a seat up to the point where real personal data would be entered. It does not fill in names, it does not pay, and it never will.

---

## Credit

Built on [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast), which contributed the CDP driving loop and the indexed action space. The agent harness, Jev routing, fare picker, live UI, and timeline are this project's own.

MIT, same as upstream.
