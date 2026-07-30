"""Adapter turning Home Assistant triggers into normalised engine signals.

This is the only layer that knows about Home Assistant's trigger machinery. It
exists separately from the entity because the phantom-edge filtering below is
Home Assistant domain knowledge that has no place in a pure state machine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

import voluptuous as vol

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.trigger import (
    async_initialize_triggers,
    async_validate_trigger_config,
)
from homeassistant.helpers.typing import TemplateVarsType

from .const import DOMAIN, SHAPE_DECODED, SHAPE_EDGE, SHAPE_SINGLE_SHOT
from .gestures import DecodedGesture, RawSignal
from .model import ButtonConfig, SourceConfig

_LOGGER = logging.getLogger(__name__)

type Payload = RawSignal | DecodedGesture
type SignalCallback = Callable[[Payload, str], None]

_INVALID_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _trigger_groups(
    source: SourceConfig,
) -> Iterator[tuple[tuple[dict[str, Any], ...], Payload]]:
    """Yield (raw triggers, what they mean) for one source."""
    if source.shape == SHAPE_EDGE:
        yield source.press_triggers, RawSignal.DOWN
        yield source.release_triggers, RawSignal.UP
    elif source.shape == SHAPE_SINGLE_SHOT:
        yield source.triggers, RawSignal.TAP
    elif source.shape == SHAPE_DECODED and source.gesture is not None:
        yield source.triggers, source.gesture


@callback
def _is_phantom_edge(run_variables: TemplateVarsType) -> bool:
    """True for a state change out of unavailable/unknown.

    A state trigger with `to: "on"` also fires on `unavailable -> on`, which is
    what happens when the source entity restores at startup. Left alone that
    looks like a press, and half a second later like the start of a hold. Only
    state-based triggers carry from_state, so event triggers are unaffected.
    """
    trigger = (run_variables or {}).get("trigger") or {}
    if "from_state" not in trigger:
        return False
    from_state = trigger["from_state"]
    return from_state is None or from_state.state in _INVALID_STATES


async def _async_attach_group(
    hass: HomeAssistant,
    raw_triggers: tuple[dict[str, Any], ...],
    name: str,
    source_id: str,
    payload: Payload,
    on_signal: SignalCallback,
) -> CALLBACK_TYPE | None:
    """Attach one group of triggers that all mean the same thing."""
    if not raw_triggers:
        return None

    try:
        validated = await async_validate_trigger_config(
            hass, cv.TRIGGER_SCHEMA(list(raw_triggers))
        )
    except (vol.Invalid, HomeAssistantError) as err:
        # One bad trigger must not take the whole remote down.
        _LOGGER.error(
            "%s: ignoring invalid trigger configuration for %s: %s",
            name,
            source_id,
            err,
        )
        return None

    @callback
    def _handle(
        run_variables: TemplateVarsType, context: Context | None = None
    ) -> None:
        """Forward one signal to the engine.

        Deliberately a callback rather than a coroutine: an async action is
        scheduled as a task, which could invert a DOWN/UP pair that arrived in
        quick succession. Synchronous dispatch preserves bus order.
        """
        if _is_phantom_edge(run_variables):
            _LOGGER.debug(
                "%s: ignoring %s from %s (source was unavailable)",
                name,
                payload,
                source_id,
            )
            return
        on_signal(payload, source_id)

    return await async_initialize_triggers(
        hass, validated, _handle, DOMAIN, name, _LOGGER.log
    )


async def async_attach_sources(
    hass: HomeAssistant,
    config: ButtonConfig,
    name: str,
    on_signal: SignalCallback,
) -> list[CALLBACK_TYPE]:
    """Attach every source for one button, returning their unsubscribes."""
    unsubs: list[CALLBACK_TYPE] = []

    for index, source in enumerate(config.sources):
        source_id = f"source{index}({source.shape})"
        for raw_triggers, payload in _trigger_groups(source):
            unsub = await _async_attach_group(
                hass, raw_triggers, name, source_id, payload, on_signal
            )
            if unsub is not None:
                unsubs.append(unsub)

    return unsubs
