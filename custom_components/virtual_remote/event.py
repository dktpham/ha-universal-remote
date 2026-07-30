"""Event entities for virtual remote buttons."""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import CONF_BUTTONS, DOMAIN
from .gestures import DecodedGesture, EngineStep, GestureEngine
from .model import ButtonConfig, derive_event_types, translate
from .source import Payload, async_attach_sources

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one event entity per button of every configured remote."""
    for subentry_id, subentry in entry.subentries.items():
        entities = _build_entities(subentry_id, subentry)
        if entities:
            # config_subentry_id ties the entities to the subentry so that
            # removing the remote cleans up its device and entities for us.
            async_add_entities(entities, config_subentry_id=subentry_id)


def _build_entities(
    subentry_id: str, subentry: ConfigSubentry
) -> list[VirtualRemoteButtonEvent]:
    """Build the entities for one virtual remote, skipping broken buttons."""
    entities: list[VirtualRemoteButtonEvent] = []

    for button_id, raw in (subentry.data.get(CONF_BUTTONS) or {}).items():
        try:
            config = ButtonConfig.from_dict(raw)
        except (KeyError, ValueError) as err:
            _LOGGER.error(
                "Skipping button %s of %s: %s", button_id, subentry.title, err
            )
            continue
        entities.append(
            VirtualRemoteButtonEvent(subentry_id, subentry.title, button_id, config)
        )

    return entities


class VirtualRemoteButtonEvent(EventEntity):
    """One button of a virtual remote."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        subentry_id: str,
        remote_name: str,
        button_id: str,
        config: ButtonConfig,
    ) -> None:
        """Initialise the entity and its engine."""
        self._config = config
        self._engine = GestureEngine(
            config.gesture_config, name=f"{remote_name}/{config.name}"
        )
        self._attr_event_types = derive_event_types(config)
        self._attr_name = config.name
        self._attr_unique_id = f"{subentry_id}_{button_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry_id)},
            name=remote_name,
            manufacturer="Virtual Remote",
        )
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._unsub_sources: list[CALLBACK_TYPE] = []

    async def async_added_to_hass(self) -> None:
        """Attach the hardware sources."""
        await super().async_added_to_hass()
        self._unsub_sources = await async_attach_sources(
            self.hass, self._config, self.entity_id, self._handle_signal
        )

    async def async_will_remove_from_hass(self) -> None:
        """Detach everything, symmetrically with async_added_to_hass."""
        self._cancel_timer()
        for unsub in self._unsub_sources:
            unsub()
        self._unsub_sources.clear()
        # Discard rather than flush: there is nobody left to tell, and writing
        # state for an entity about to vanish is pointless.
        self._engine.reset(flush=False)
        await super().async_will_remove_from_hass()

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _apply(self, step: EngineStep, now: float) -> None:
        """Emit a step's gestures and re-arm the engine's single timer.

        The only place where time, Home Assistant and the engine meet.
        """
        self._cancel_timer()

        for emission in step.emissions:
            event_type, attributes = translate(emission)
            if event_type not in self.event_types:
                # Guarded by a unit-test invariant over the whole config matrix,
                # so this is a bug in derive_event_types rather than bad input.
                _LOGGER.error(
                    "%s: refusing to emit undeclared event type %s",
                    self.entity_id,
                    event_type,
                )
                continue
            self._trigger_event(event_type, attributes)
            # After EACH emission: _trigger_event only updates private fields,
            # so batching one write at the end would silently discard every
            # event but the last. A click-then-hold emits two in one step.
            self.async_write_ha_state()

        if step.deadline is not None:
            self._unsub_timer = async_call_later(
                self.hass, max(0.0, step.deadline - now), self._handle_deadline
            )

    @callback
    def _handle_deadline(self, fire_time: datetime) -> None:
        """The engine asked to be woken at this instant."""
        self._unsub_timer = None  # first statement: the handle is already spent
        now = fire_time.timestamp()
        self._apply(self._engine.tick(now), now)

    @callback
    def _handle_signal(self, payload: Payload, source_id: str) -> None:
        """One normalised signal from a hardware source."""
        now = dt_util.utcnow().timestamp()
        if isinstance(payload, DecodedGesture):
            step = self._engine.decoded(payload, now)
        else:
            step = self._engine.signal(payload, now, source_id)
        self._apply(step, now)
