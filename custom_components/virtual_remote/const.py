"""Constants for the Virtual Remote integration.

Pure module: no homeassistant imports, so it can be unit-tested without a
running Home Assistant.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "virtual_remote"

# --- Button event types ------------------------------------------------------
# These mirror homeassistant.components.event.const.ButtonEventType, which
# landed in core on 2026-07-22 (PR #177028) and first ships in 2026.8. They are
# duplicated here as plain strings so this integration also runs on 2026.7,
# where the enum does not exist yet - EventEntity._trigger_event() takes a str
# either way. On 2026.8+ the frontend supplies translated labels for free.
# Replace these with the real import once the minimum HA version reaches 2026.8.
EVENT_PRESS_START: Final = "press_start"
EVENT_PRESS_END: Final = "press_end"
EVENT_LONG_PRESS_START: Final = "long_press_start"
EVENT_LONG_PRESS_END: Final = "long_press_end"
EVENT_MULTI_PRESS_ONGOING: Final = "multi_press_ongoing"
EVENT_MULTI_PRESS_END: Final = "multi_press_end"

# Declaration order is significant: derive_event_types() orders its output by
# this sequence so that the entity's capability attributes stay byte-stable
# across restarts (they are recorded in the entity registry).
BUTTON_EVENT_TYPES: Final = (
    EVENT_PRESS_START,
    EVENT_PRESS_END,
    EVENT_LONG_PRESS_START,
    EVENT_LONG_PRESS_END,
    EVENT_MULTI_PRESS_ONGOING,
    EVENT_MULTI_PRESS_END,
)

# Mirrors homeassistant.components.event.ATTR_MULTI_PRESS_COUNT.
ATTR_MULTI_PRESS_COUNT: Final = "multi_press_count"

# --- Stored configuration keys ----------------------------------------------
CONF_BUTTONS: Final = "buttons"
CONF_NAME: Final = "name"
CONF_SOURCES: Final = "sources"
CONF_SHAPE: Final = "shape"
CONF_TRIGGERS: Final = "triggers"
CONF_PRESS_TRIGGERS: Final = "press_triggers"
CONF_RELEASE_TRIGGERS: Final = "release_triggers"
CONF_GESTURE: Final = "gesture"
CONF_LONG_PRESS_THRESHOLD: Final = "long_press_threshold"
CONF_MULTI_PRESS_MAX: Final = "multi_press_max"
CONF_MULTI_PRESS_WINDOW: Final = "multi_press_window"
CONF_MULTI_PRESS_IMMEDIATE: Final = "multi_press_immediate"

# --- Source shapes ----------------------------------------------------------
# edge        - separate press and release signals; duration is measurable
# single_shot - one event per interaction, no release (most Zigbee scene buttons)
# decoded     - hardware already reports the gesture; pass through unchanged
SHAPE_EDGE: Final = "edge"
SHAPE_SINGLE_SHOT: Final = "single_shot"
SHAPE_DECODED: Final = "decoded"

SHAPES: Final = (SHAPE_EDGE, SHAPE_SINGLE_SHOT, SHAPE_DECODED)

# --- Event types watched during the config-flow learning step ---------------
# An allowlist rather than MATCH_ALL: MATCH_ALL makes every event on the bus
# allocate and dispatch to us, and it never delivers EVENT_STATE_REPORTED
# anyway (see EVENTS_EXCLUDED_FROM_MATCH_ALL in homeassistant/core.py).
LEARNABLE_EVENT_TYPES: Final = (
    "zha_event",
    "deconz_event",
    "xiaomi_aqara.click",
    "tuya_event",
    "keyboard_remote_command_received",
    "state_changed",
)
