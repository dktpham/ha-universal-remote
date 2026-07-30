# Virtual Remote

Turn any button device into a standard Home Assistant remote — regardless of
whether it arrived via ZHA, Zigbee2MQTT, deCONZ, Thread or anything else — and
get gestures the hardware never reported, like double press.

> **Status: not yet run in Home Assistant.** The gesture engine has 137 passing
> tests, and every Home Assistant API used is statically resolved against a core
> checkout, but no part of this has been loaded into a live instance. Treat the
> first install as the real test. See [Status](#status-and-limits).

## How it works

Two layers. The integration normalises hardware into standard entities; a
blueprint maps those to actions.

```
  ZHA / Zigbee2MQTT / deCONZ / Thread / Matter / ESPHome / a wall switch
                              │
                    ┌─────────▼─────────────┐
                    │ virtual_remote        │  learns each button by having
                    │ (custom integration)  │  you press it; synthesises
                    └─────────┬─────────────┘  gestures the hardware lacks
                              │
                    event.living_room_button_1   ← one entity per button,
                      device_class: button          device_class: button
                      event_type: multi_press_end
                      multi_press_count: 2
                              │
                    ┌─────────▼─────────────┐
                    │ Button actions        │  one automation per button
                    │ (blueprint)           │
                    └───────────────────────┘
```

The integration is the only thing that touches hardware. Everything downstream
sees a standard button entity, so a virtual remote is indistinguishable from a
natively-supported one.

### The event vocabulary

Home Assistant standardised button events in core PR
[#177028](https://github.com/home-assistant/core/pull/177028) (2026-07-22,
shipping in **2026.8**):

| Event type | Meaning |
|---|---|
| `press_start` | the button went down |
| `press_end` | released after a brief press — the standard click |
| `long_press_start` | held past the threshold |
| `long_press_end` | released after a long hold |
| `multi_press_ongoing` | an intermediate press in a sequence |
| `multi_press_end` | a sequence completed |

Multi-press is **one event type carrying a `multi_press_count` attribute**, not
separate double/triple types — so quadruple press comes free. This integration
appears to be the first implementer of these types.

## Install

**Integration** — copy `custom_components/virtual_remote/` into your Home
Assistant `config/custom_components/`, then restart. Add it via
**Settings → Devices & services → Add integration → Virtual Remote**.

**Blueprint** — copy
`blueprints/automation/dktpham/virtual_remote_actions.yaml` into
`config/blueprints/automation/dktpham/`, then **Developer tools → YAML →
Reload blueprints**.

## Setup

### 1. Add a remote

**Add virtual remote** on the integration's entry, then:

1. Name it and say how many buttons it has.
2. For each button, a dialog says *"Press and release Button 1 now"* and
   captures whatever hits the event bus. It shows what it caught and what it
   inferred, and you accept, retry, or type it in by hand.
3. Optionally *"also learn hold"* — for remotes that report holds themselves
   (an IKEA dim button sends different events for a tap and a hold), this adds
   the hold as a pass-through gesture.
4. Finally, choose whether to detect multi-presses.

You end up with one device and one `event` entity per button. Nothing to type
but the name — no canonical gesture vocabulary, no trigger IDs, no YAML.

**What "shape" means.** The flow infers it, but it drives everything downstream:

| Captured | Shape | Consequence |
|---|---|---|
| two events per press | `edge` | duration is measurable → long press can be synthesised |
| one event per press | `single_shot` | no duration → no `press_start`, no synthesised holds |
| a hold you taught it | `decoded` | passed through as-is; the hardware already decided |

### 2. Map it to actions

Create an automation from **Virtual Remote · Button actions**, pick the button
entity, and fill in the gestures you want. One automation per button.

## The multi-press latency trade

Synthesising a double press has an unavoidable cost: a single press cannot be
reported as final until the window closes, because it might turn out to be the
first of two.

The mitigation is **terminate-on-max** — once the configured maximum number of
presses has landed the sequence is provably complete, so the *highest* gesture
you configure is always instant and only shorter ones wait:

| Gesture | detect up to 1 | up to 2 | up to 3 |
|---|---|---|---|
| single press | **instant** | +400 ms | +400 ms |
| double press | — | **instant** | +400 ms |
| hold, release, pre-decoded | **instant** | **instant** | **instant** |

Multi-press defaults to **off** for that reason: one press to toggle a light is
the common case, and 400 ms of added latency there is a bad default. When you do
enable it, "up to double" keeps the double instant.

With multi-press on, each non-final press still emits `multi_press_ongoing`
immediately, so a single-shot button gives feedback without waiting.

## Status and limits

**Never run in Home Assistant.** What *is* verified:

- The gesture engine, thoroughly — 137 tests, no Home Assistant needed.
  `tools/check_integration.py` enforces that `gestures.py`, `model.py` and
  `const.py` import nothing from `homeassistant`, which is what makes that
  possible.
- Every core import statically resolved against a checkout, with anything
  unresolvable reported rather than passed silently.

What is **not** verified: that it loads, that the config flow renders, that the
learning step captures anything. Those need a live instance.

Known limits:

- **Buttons cannot be added or removed after creation.** Reconfigure is not
  implemented in v1; delete the remote and re-add it. (Deliberate: removing a
  button needs orphaned-entity cleanup, and half-doing that leaves stale
  entities behind.)
- **One source per button per learning pass**, plus an optional hold. A button
  whose short press and hold arrive as unrelated events is handled; a button fed
  from two different physical devices is not, in the flow — the data model and
  engine support it, the UI does not yet.
- **`double`/`triple` need either hardware support or synthesis enabled.**
- **No dials.** Rotation is not expressible on a `device_class: button` entity;
  it needs its own entity type.
- **On Home Assistant 2026.7** the event types work but display untranslated,
  since the translations ship with 2026.8. Self-healing on upgrade.
- **Timing defaults** (400 ms window, 500 ms long-press threshold, 50 ms tap
  coalescing) come from desktop and Android human-factors defaults, *not* from
  measurements of Zigbee remotes.

### An upstream gap

`event.received` can only filter on `event_type`, not on attributes, so "on
double press" needs a template condition on `multi_press_count`. The blueprint
hides it, but the real fix is an optional `multi_press_count` filter in
`EVENT_RECEIVED_TRIGGER_SCHEMA` upstream. Inventing `double_press` event types
instead would forfeit exactly the standardisation this integration exists to
provide.

## Development

```bash
python -m venv .venv
.venv/Scripts/python -m pip install pytest pyyaml

.venv/Scripts/python -m pytest tests/ -q          # engine + strings coverage
.venv/Scripts/python tools/check_integration.py   # purity + import resolution
.venv/Scripts/python tools/check_blueprints.py blueprints
```

`check_integration.py` takes an optional path to a Home Assistant checkout (or
reads `$HA_CORE`). Without one it still enforces engine purity.

Architecture, in dependency order:

| File | Role |
|---|---|
| `gestures.py` | the state machine. **Zero `homeassistant` imports** — holds no clock and arms no timers, so it is a pure function of `(state, signal, now)` |
| `model.py` | typed config, and the engine → Home Assistant event-type mapping |
| `source.py` | attaches user triggers, normalises them to engine signals |
| `event.py` | the entity: owns the single timer and calls `_trigger_event` |
| `config_flow.py` | setup, including the learn-by-pressing step |

The engine boundary is load-bearing, not stylistic: it is what lets the timing
logic be tested exhaustively in 0.1 s, and it means the engine could move to a
standalone package unchanged.

## Legacy blueprints

`blueprints/automation/dktpham/virtual_remote_{adapter,4button,5button,*_adapter}.yaml`
are the **superseded** pure-blueprint implementation: an adapter automation
normalised hardware into `virtual_remote` bus events, and a topology dispatcher
mapped those to actions. It works — one setup was tested on a ZHA IKEA TRADFRI —
but it needed two automations per remote, a matching slug between them, and it
could not synthesise gestures at all, because blueprints are stateless and their
forms are static.

Kept for reference and for anyone who cannot install a custom component. The
integration replaces both halves.
