# Universal HA Remote Blueprints

Map any button device to any action, without the blueprint knowing or caring
which integration the device came from.

## The idea

Two layers, joined by a canonical vocabulary on the event bus:

```
   ZHA / Zigbee2MQTT / deCONZ / Thread / Matter / ESPHome / a wall switch
                              │
                    ┌─────────▼──────────┐
                    │  Adapter blueprint │   one per physical remote
                    │  "the driver"      │   maps hardware → canonical
                    └─────────┬──────────┘
                              │  event: virtual_remote
                              │  { remote: living_room, gesture: b2_long }
                    ┌─────────▼──────────┐
                    │ Dispatcher         │   one per remote
                    │ blueprint          │   canonical → your actions
                    └────────────────────┘
```

The adapter is the only place that touches hardware. The dispatcher only ever
sees `b2_long`, so it works identically on a ZHA TRADFRI and a Zigbee2MQTT
STYRBAR — and on hardware that does not exist yet.

### Canonical vocabulary

| | |
|---|---|
| Buttons | `b1` `b2` `b3` `b4` `b5` `b6` `b7` `b8`, plus `dial` |
| Gestures | `short` `double` `triple` `long` `release` `cw` `ccw` |
| Composed | `b1_short` · `b3_long` · `b3_release` · `dial_cw` |

`long` fires when a hold *begins*; `release` when it ends. A remote that cannot
report a release just has no `*_release` triggers — hold-to-repeat then stops at
its step cap instead.

Slots are deliberately generic. `b1`…`b5` stays true across every device you
will ever map; `up`/`down` stops being true the moment you map a cube or a
two-button remote.

## Install

Copy into your HA config, or add via **Settings → Automations & Scenes →
Blueprints → Import Blueprint**:

```
config/blueprints/automation/dktpham/virtual_remote_adapter.yaml
config/blueprints/automation/dktpham/virtual_remote_4button.yaml
config/blueprints/automation/dktpham/virtual_remote_5button.yaml
```

The adapter is always required. Add whichever dispatcher matches the remote's
button count.

Then **Developer Tools → YAML → Reload Blueprints** (or restart).

## Setup

### Step 1 — the adapter

Create an automation from **Virtual Remote · Adapter**.

1. **Virtual remote ID** — a slug, e.g. `living_room_5button`.
2. **Hardware button events** — add one trigger per gesture. Use whatever your
   integration offers: a device trigger, a raw `zha_event`, an MQTT topic, a
   state change. Mix freely.
3. **For every trigger, set its Trigger ID** to a canonical gesture name.

> ### ⚠ The one rule
> Every trigger needs a **Trigger ID** and it must be a canonical gesture name
> (`b1_short`, `b2_release`, …). In the trigger's **⋮ menu → Edit ID**.
>
> If you skip it, HA silently falls back to the trigger's *position* — `"0"`,
> `"1"`, `"2"` — and the dispatcher will never match. This is the single most
> likely reason a setup does not work.

The Trigger ID *is* the mapping. That is the whole trick: all hardware
weirdness stays inside the trigger, and the ID is the contract.

### Step 2 — the dispatcher

Create an automation from **Virtual Remote · 5-button dispatcher**, give it the
**same** remote ID, and fill in the actions you want per button and gesture.

Enable **Repeat while held** under *Hold behaviour* if you want hold-to-dim or
hold-to-run-a-cover. The release event cancels the loop (the blueprint runs in
`mode: restart`, which is what makes that work).

## Finding your hardware events

**Preferred — device triggers.** In the trigger editor pick *Device*, choose the
remote, and use the dropdown. Names like "Remote button short press · turn on"
are already integration-agnostic and need no cluster knowledge. Not every
integration exposes a *release*, though.

**Fallback — watch the bus.** Developer Tools → **Events** → listen to
`zha_event` (or `deconz_event`, or `mqtt` for Zigbee2MQTT), press buttons, and
read what arrives. Then use an *Event* trigger matching what you saw.

### IKEA TRADFRI 5-button (E1524/E1810) under ZHA

These signatures are taken from your own working blueprint, so the marked rows
are known-good on this hardware. Suggested slots: `b1` centre/power, `b2` up,
`b3` down, `b4` left, `b5` right.

| Trigger ID | `zha_event` command | args | verified |
|---|---|---|---|
| `b1_short`   | `toggle` | | ✅ |
| `b1_long`    | `move_to_level_with_on_off` | | ✅ |
| `b2_short`   | `step_with_on_off` | | ✅ |
| `b2_long`    | `move_with_on_off` | | ✅ |
| `b3_short`   | `step` | | ✅ |
| `b3_long`    | `move` | | ✅ |
| `b4_short`   | `press` | `[257, 13, 0]` | ✅ |
| `b4_long`    | `hold`  | `[3329, 0]` | ✅ |
| `b5_short`   | `press` | `[256, 13, 0]` | ✅ |
| `b5_long`    | `hold`  | `[3328, 0]` | ✅ |
| `b2_release` | `stop_with_on_off` | | ⚠ inferred |
| `b3_release` | `stop` | | ⚠ inferred |

The two release rows follow the Zigbee LevelControl pairing
(`move_with_on_off`→`stop_with_on_off`, `move`→`stop`) rather than direct
observation — confirm with the event listener. Left/right release (`b4_release`,
`b5_release`) is not in your current config at all; watch the bus for a
`release` command if you want it.

An event trigger for a raw signature looks like:

```yaml
trigger: event
event_type: zha_event
event_data:
  device_id: <your remote's device id>
  command: press
  args: [256, 13, 0]
id: b5_short          # ← the canonical gesture
```

### STYRBAR (E2001/E2002)

Four buttons. Use the **4-button dispatcher** and map `b1` up/on, `b2` down/off,
`b3` left, `b4` right. Its ZHA device triggers cover short and long press; verify
release with the event listener.

If you own both a STYRBAR and a 5-button TRADFRI and want the same physical
position to mean the same slot on both, use the **5-button** dispatcher for the
STYRBAR instead and leave `b1` (centre) unused — up/down/left/right then line up
as `b2`…`b5` on each remote. Pick one convention per household and stick to it;
the slot numbers live in the adapter's Trigger IDs, so changing your mind later
means editing the adapter, not just swapping dispatchers.

## Testing

1. Set up the adapter, then watch Developer Tools → **Events** → listen to
   `virtual_remote` and press buttons. You should see
   `{ remote: …, gesture: b2_short, value: "" }`.
2. If gestures come through as `"0"`, `"1"`, … you forgot the Trigger IDs.
3. If nothing arrives, the hardware trigger itself is wrong — check the
   adapter automation's trace.
4. Only once step 1 looks right, wire up the dispatcher.

Debugging is genuinely nicer than a monolithic blueprint here: the bus event is
an inspectable seam, so you always know which layer is at fault.

## Limits, honestly

- **Two automations per remote**, and you type Trigger IDs by hand. That is the
  cost of not having per-model tables. The planned custom integration removes it.
- **No virtual-remote entity.** The canonical events are bus events, not an
  entity, so there is nothing to see in the UI. (A template *event entity* would
  fix this, but trigger-based template entities are YAML-only — no UI path at
  all — which is worse fiddling for less gain.)
- **`double`/`triple` need hardware support.** The blueprint does not synthesise
  them from timing; if your remote does not report a double press, that slot
  stays unused.
- **Dial magnitude** rides along in `value` via the adapter's value-passthrough
  template, available to your actions as the `value` variable. It has to be
  written defensively — it runs on *every* gesture.

## Roadmap

- Dispatchers for 1/2-button and dial topologies
- Port the target-stepping "room control" pattern (step through lights/covers,
  presets, confirmation blip) as a second dispatcher
- **Custom integration**: a config flow that says "press button 1 now", captures
  whatever hits the bus, and writes the mapping for you — no Trigger IDs, no
  adapter automation, and a real device with an event entity
