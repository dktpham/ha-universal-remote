"""Typed view of the stored button configuration, and the event-type mapping.

Pure module: no homeassistant imports. It sits between the engine's abstract
vocabulary and Home Assistant's ButtonEventType strings, and lives here rather
than in event.py so that derive_event_types() and the translation table can be
unit-tested without a running Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import (
    ATTR_MULTI_PRESS_COUNT,
    BUTTON_EVENT_TYPES,
    CONF_GESTURE,
    CONF_LONG_PRESS_THRESHOLD,
    CONF_MULTI_PRESS_IMMEDIATE,
    CONF_MULTI_PRESS_MAX,
    CONF_MULTI_PRESS_WINDOW,
    CONF_NAME,
    CONF_PRESS_TRIGGERS,
    CONF_RELEASE_TRIGGERS,
    CONF_SHAPE,
    CONF_SOURCES,
    CONF_TRIGGERS,
    EVENT_LONG_PRESS_END,
    EVENT_LONG_PRESS_START,
    EVENT_MULTI_PRESS_END,
    EVENT_MULTI_PRESS_ONGOING,
    EVENT_PRESS_END,
    EVENT_PRESS_START,
    SHAPE_DECODED,
    SHAPE_EDGE,
    SHAPE_SINGLE_SHOT,
)
from .gestures import DecodedGesture, Emission, GestureConfig, GestureEmission


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """One hardware source feeding a button."""

    shape: str
    triggers: tuple[dict[str, Any], ...] = ()
    """For single_shot and decoded shapes."""
    press_triggers: tuple[dict[str, Any], ...] = ()
    release_triggers: tuple[dict[str, Any], ...] = ()
    """For the edge shape."""
    gesture: DecodedGesture | None = None
    """For the decoded shape."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceConfig:
        """Build from the stored representation."""
        shape = data[CONF_SHAPE]
        gesture = data.get(CONF_GESTURE)
        return cls(
            shape=shape,
            triggers=tuple(data.get(CONF_TRIGGERS) or ()),
            press_triggers=tuple(data.get(CONF_PRESS_TRIGGERS) or ()),
            release_triggers=tuple(data.get(CONF_RELEASE_TRIGGERS) or ()),
            gesture=DecodedGesture(gesture) if gesture else None,
        )


@dataclass(frozen=True, slots=True)
class ButtonConfig:
    """Everything needed to build one button's entity and engine."""

    name: str
    sources: tuple[SourceConfig, ...]
    gesture_config: GestureConfig

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ButtonConfig:
        """Build from a stored subentry button dict."""
        sources = tuple(
            SourceConfig.from_dict(src) for src in data.get(CONF_SOURCES) or ()
        )
        if not sources:
            # event_types would be empty and EventEntity.event_types would
            # raise, so refuse loudly here instead.
            raise ValueError(f"button {data.get(CONF_NAME)!r} has no sources")

        defaults = GestureConfig()
        return cls(
            name=data[CONF_NAME],
            sources=sources,
            gesture_config=GestureConfig(
                # Absent means "do not synthesise long presses"; there is
                # deliberately no separate boolean for it.
                long_press_threshold=data.get(CONF_LONG_PRESS_THRESHOLD),
                multi_press_window=data.get(
                    CONF_MULTI_PRESS_WINDOW, defaults.multi_press_window
                ),
                multi_press_max=data.get(
                    CONF_MULTI_PRESS_MAX, defaults.multi_press_max
                ),
                multi_press_immediate=data.get(
                    CONF_MULTI_PRESS_IMMEDIATE, defaults.multi_press_immediate
                ),
            ),
        )

    @property
    def shapes(self) -> frozenset[str]:
        """The distinct source shapes feeding this button."""
        return frozenset(src.shape for src in self.sources)


# Which Home Assistant event type a pre-decoded gesture maps onto.
DECODED_EVENT_TYPES: dict[DecodedGesture, str] = {
    DecodedGesture.CLICK: EVENT_PRESS_END,
    DecodedGesture.DOUBLE: EVENT_MULTI_PRESS_END,
    DecodedGesture.TRIPLE: EVENT_MULTI_PRESS_END,
    DecodedGesture.QUADRUPLE: EVENT_MULTI_PRESS_END,
    DecodedGesture.HOLD_START: EVENT_LONG_PRESS_START,
    DecodedGesture.HOLD_END: EVENT_LONG_PRESS_END,
}

# The engine's abstract vocabulary translated to Home Assistant's.
EMISSION_EVENT_TYPES: dict[Emission, str] = {
    Emission.PRESS_DOWN: EVENT_PRESS_START,
    Emission.CLICK_PROGRESS: EVENT_MULTI_PRESS_ONGOING,
    Emission.CLICK_FINAL: EVENT_PRESS_END,
    Emission.MULTI_FINAL: EVENT_MULTI_PRESS_END,
    Emission.HOLD_START: EVENT_LONG_PRESS_START,
    Emission.HOLD_END: EVENT_LONG_PRESS_END,
}

# Only these carry the press count. A terminal event never reports a count of
# one: upstream calls press_end "the standard click", and one press is not a
# sequence.
_COUNTED_EMISSIONS = frozenset({Emission.CLICK_PROGRESS, Emission.MULTI_FINAL})


def translate(emission: GestureEmission) -> tuple[str, dict[str, Any] | None]:
    """Convert one engine emission into (event_type, event_attributes)."""
    event_type = EMISSION_EVENT_TYPES[emission.kind]
    if emission.kind in _COUNTED_EMISSIONS and emission.count is not None:
        return event_type, {ATTR_MULTI_PRESS_COUNT: emission.count}
    return event_type, None


def derive_event_types(config: ButtonConfig) -> list[str]:
    """The exact set of event types this button can ever emit.

    EventEntity._trigger_event() raises if handed a type the entity did not
    declare, so this must be a superset of what the engine can produce for this
    configuration. The unit tests assert exactly that over the full config
    matrix, which is what makes that ValueError unreachable in production.
    """
    types: set[str] = set()
    shapes = config.shapes
    gestures = config.gesture_config

    if SHAPE_EDGE in shapes:
        # Only an edge source reports the button going down.
        types.add(EVENT_PRESS_START)
        if gestures.long_press_threshold is not None:
            types.update({EVENT_LONG_PRESS_START, EVENT_LONG_PRESS_END})

    if shapes & {SHAPE_EDGE, SHAPE_SINGLE_SHOT}:
        types.add(EVENT_PRESS_END)
        if gestures.multi_press_max >= 2:
            types.add(EVENT_MULTI_PRESS_END)
            if gestures.multi_press_immediate:
                types.add(EVENT_MULTI_PRESS_ONGOING)

    for src in config.sources:
        if src.shape == SHAPE_DECODED and src.gesture is not None:
            types.add(DECODED_EVENT_TYPES[src.gesture])
            if src.gesture is DecodedGesture.HOLD_START:
                # A decoded hold start arms the stuck timer, which can
                # synthesise the terminator if the hardware's hold-end is lost.
                types.add(EVENT_LONG_PRESS_END)

    # Ordered by declaration, not sorted(): capability attributes are recorded
    # in the entity registry, so an unstable order churns on every restart.
    return [event_type for event_type in BUTTON_EVENT_TYPES if event_type in types]
