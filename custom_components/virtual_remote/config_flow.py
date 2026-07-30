"""Config flow for Virtual Remote, including learn-by-pressing.

The learning step subscribes to the event bus while a progress dialog is shown,
captures whatever the remote emits, and proposes a trigger from it. Nothing in
Home Assistant core does this from a flow, so the shape is modelled on
components/improv_ble (subscribe -> asyncio.Event -> progress task ->
unsubscribe) and components/snooz (progress, timeout, retry loop).

Teardown deserves a note: `async_remove()` is not documented in the data-entry-
flow reference, but it is the only hook that fires when a user abandons a flow
mid-step, and core uses it for exactly this (components/lg_netcast). Without it
an abandoned learning step would leave a bus subscription behind.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, callback
from homeassistant.helpers import selector
from homeassistant.helpers.event import async_call_later
from homeassistant.util import slugify
from homeassistant.util.ulid import ulid_now

from .const import (
    CONF_BUTTONS,
    CONF_GESTURE,
    CONF_LONG_PRESS_THRESHOLD,
    CONF_MULTI_PRESS_MAX,
    CONF_NAME,
    CONF_PRESS_TRIGGERS,
    CONF_RELEASE_TRIGGERS,
    CONF_SHAPE,
    CONF_SOURCES,
    CONF_TRIGGERS,
    DOMAIN,
    LEARNABLE_EVENT_TYPES,
    SHAPE_DECODED,
    SHAPE_EDGE,
    SHAPE_SINGLE_SHOT,
)
from .gestures import DecodedGesture

_LOGGER = logging.getLogger(__name__)

# How long to wait for the user to press something before giving up.
LEARN_TIMEOUT = 25.0
# After the first event arrives, keep listening this long for a second one. That
# is what distinguishes a press/release pair from a single-shot button without
# making a single-shot button wait out the whole timeout.
SETTLE_WINDOW = 1.5

MAX_BUTTONS = 8

# Event data keys that identify *which* control was operated. Everything else
# (timestamps, sequence numbers, signal strength) is noise that would stop the
# generated trigger from matching next time.
_IDENTIFYING_KEYS = (
    "device_id",
    "device_ieee",
    "unique_id",
    "endpoint_id",
    "cluster_id",
    "command",
    "args",
    "type",
    "subtype",
    "event",
    "action",
    "click_type",
)

_INVALID_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _trigger_from_event(event: Event) -> dict[str, Any] | None:
    """Build a trigger that will match this event again, or None to ignore it."""
    if event.event_type == "state_changed":
        data = event.data
        from_state, to_state = data.get("old_state"), data.get("new_state")
        if to_state is None or to_state.state in _INVALID_STATES:
            return None
        # An attribute-only change is not a button press.
        if from_state is not None and from_state.state == to_state.state:
            return None
        return {
            "trigger": "state",
            "entity_id": data["entity_id"],
            "to": to_state.state,
        }

    identifying = {
        key: value for key, value in event.data.items() if key in _IDENTIFYING_KEYS
    }
    if not identifying:
        return None
    return {
        "trigger": "event",
        "event_type": event.event_type,
        "event_data": identifying,
    }


def _describe(trigger: dict[str, Any]) -> str:
    """One-line human summary of a generated trigger."""
    if trigger.get("trigger") == "state":
        return f"{trigger['entity_id']} → {trigger['to']}"
    data = trigger.get("event_data", {})
    detail = ", ".join(f"{key}={value}" for key, value in data.items())
    return f"{trigger['event_type']} ({detail})"


class VirtualRemoteConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create the single config entry that owns every virtual remote."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry; remotes are added afterwards as subentries."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return self.async_create_entry(title="Virtual Remotes", data={})

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """One subentry type: a virtual remote."""
        return {"remote": RemoteSubentryFlowHandler}


class RemoteSubentryFlowHandler(ConfigSubentryFlow):
    """Add one virtual remote, learning each button by having it pressed."""

    def __init__(self) -> None:
        """Initialise the flow state."""
        self._name = ""
        self._button_count = 5
        self._index = 0
        self._buttons: dict[str, dict[str, Any]] = {}

        # Per-button accumulation.
        self._sources: list[dict[str, Any]] = []
        self._press_triggers: list[dict[str, Any]] = []

        # Capture machinery.
        self._captures: list[dict[str, Any]] = []
        self._unsub_bus: CALLBACK_TYPE | None = None
        self._unsub_settle: CALLBACK_TYPE | None = None
        self._captured = asyncio.Event()
        self._capture_task: asyncio.Task[None] | None = None
        self._learning_hold = False

        self._multi_press_max = 1

    # --- lifecycle ----------------------------------------------------------

    @callback
    def async_remove(self) -> None:
        """Tear down the bus subscription if the user abandons the flow.

        Undocumented but load-bearing: without it, walking away from the
        learning dialog leaks a listener for every event type we watch.
        """
        self._stop_listening()

    @callback
    def _stop_listening(self) -> None:
        if self._unsub_settle is not None:
            self._unsub_settle()
            self._unsub_settle = None
        if self._unsub_bus is not None:
            self._unsub_bus()
            self._unsub_bus = None

    # --- step 1: name and size ---------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Name the remote and say how many buttons it has."""
        if user_input is not None:
            self._name = user_input[CONF_NAME]
            self._button_count = int(user_input["button_count"])
            return await self.async_step_learn()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): selector.TextSelector(),
                    vol.Required("button_count", default=5): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=MAX_BUTTONS, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                }
            ),
        )

    # --- step 2: learn one button ------------------------------------------

    @property
    def _button_label(self) -> str:
        return f"Button {self._index + 1}"

    async def async_step_learn(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Listen for whatever the remote emits while showing a progress dialog."""
        if self._capture_task is None:
            self._captures = []
            self._captured = asyncio.Event()
            self._start_listening()
            self._capture_task = self.hass.async_create_task(
                self._async_wait_for_capture(), eager_start=False
            )

        if not self._capture_task.done():
            return self.async_show_progress(
                step_id="learn",
                progress_action="learn_hold" if self._learning_hold else "learn_press",
                progress_task=self._capture_task,
                description_placeholders={
                    "button": self._button_label,
                    "remote": self._name,
                },
            )

        timed_out = False
        try:
            await self._capture_task
        except TimeoutError:
            timed_out = True
        finally:
            self._capture_task = None
            self._stop_listening()

        if timed_out or not self._captures:
            return self.async_show_progress_done(next_step_id="learn_failed")
        return self.async_show_progress_done(next_step_id="confirm")

    @callback
    def _start_listening(self) -> None:
        """Subscribe to the event types a remote might use."""
        unsubs = [
            self.hass.bus.async_listen(event_type, self._async_on_event)
            for event_type in LEARNABLE_EVENT_TYPES
        ]

        @callback
        def _unsub_all() -> None:
            for unsub in unsubs:
                unsub()

        self._unsub_bus = _unsub_all

    async def _async_wait_for_capture(self) -> None:
        """Wait for the settle window to close, or give up."""
        async with asyncio.timeout(LEARN_TIMEOUT):
            await self._captured.wait()

    @callback
    def _async_on_event(self, event: Event) -> None:
        """Record one candidate event."""
        trigger = _trigger_from_event(event)
        if trigger is None or trigger in self._captures:
            return

        self._captures.append(trigger)
        _LOGGER.debug("Captured %s", trigger)

        # Keep listening briefly: a press/release pair arrives as two events,
        # and distinguishing that from a single-shot button is the whole point.
        if self._unsub_settle is None:
            self._unsub_settle = async_call_later(
                self.hass, SETTLE_WINDOW, self._async_settled
            )

    @callback
    def _async_settled(self, _now: Any) -> None:
        self._unsub_settle = None
        self._captured.set()

    # --- step 3: confirm what was captured ---------------------------------

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show the captured trigger(s) and let the user accept or redo."""
        if user_input is not None:
            choice = user_input["next"]
            if choice == "retry":
                return await self.async_step_learn()
            if choice == "manual":
                return await self.async_step_manual()

            self._accept_captures()
            if choice == "hold":
                self._learning_hold = True
                return await self.async_step_learn()
            return await self._async_advance()

        summary = "\n".join(f"• {_describe(t)}" for t in self._captures)
        options = [
            selector.SelectOptionDict(value="accept", label="Looks right"),
            selector.SelectOptionDict(value="retry", label="Try again"),
            selector.SelectOptionDict(value="manual", label="Enter it manually"),
        ]
        if not self._learning_hold:
            options.insert(
                1,
                selector.SelectOptionDict(
                    value="hold", label="Looks right - now also learn hold"
                ),
            )

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("next", default="accept"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options, mode=selector.SelectSelectorMode.LIST
                        )
                    )
                }
            ),
            description_placeholders={
                "button": self._button_label,
                "captured": summary,
                "shape": self._inferred_shape_label(),
            },
        )

    def _inferred_shape_label(self) -> str:
        if self._learning_hold:
            return "a hold, reported by the hardware"
        if len(self._captures) >= 2:
            return "a press and a release (duration measurable)"
        return "a single event per press (no duration available)"

    @callback
    def _accept_captures(self) -> None:
        """Turn the captured triggers into stored source configuration."""
        if self._learning_hold:
            # A hold that the hardware reports itself: the first event starts it
            # and a second, if any, ends it. Pass them through rather than
            # synthesising, since the device already decided.
            self._sources.append(
                {
                    CONF_SHAPE: SHAPE_DECODED,
                    CONF_TRIGGERS: [self._captures[0]],
                    CONF_GESTURE: DecodedGesture.HOLD_START.value,
                }
            )
            if len(self._captures) >= 2:
                self._sources.append(
                    {
                        CONF_SHAPE: SHAPE_DECODED,
                        CONF_TRIGGERS: [self._captures[1]],
                        CONF_GESTURE: DecodedGesture.HOLD_END.value,
                    }
                )
            self._learning_hold = False
            return

        if len(self._captures) >= 2:
            self._sources.append(
                {
                    CONF_SHAPE: SHAPE_EDGE,
                    CONF_PRESS_TRIGGERS: [self._captures[0]],
                    CONF_RELEASE_TRIGGERS: [self._captures[1]],
                }
            )
        else:
            self._sources.append(
                {CONF_SHAPE: SHAPE_SINGLE_SHOT, CONF_TRIGGERS: [self._captures[0]]}
            )

    # --- fallbacks ----------------------------------------------------------

    async def async_step_learn_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Nothing was captured - offer a retry, manual entry, or skip."""
        if user_input is not None:
            choice = user_input["next"]
            if choice == "retry":
                return await self.async_step_learn()
            if choice == "manual":
                return await self.async_step_manual()
            self._learning_hold = False
            return await self._async_advance(skip=not self._sources)

        return self.async_show_form(
            step_id="learn_failed",
            data_schema=vol.Schema(
                {
                    vol.Required("next", default="retry"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value="retry", label="Try again"
                                ),
                                selector.SelectOptionDict(
                                    value="manual", label="Enter it manually"
                                ),
                                selector.SelectOptionDict(
                                    value="skip", label="Skip this button"
                                ),
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={"button": self._button_label},
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Type an event signature by hand.

        The escape hatch for anything the capture step cannot see - a source
        behind a template, or a device that only changes an attribute.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            raw = (user_input.get("event_data") or "").strip()
            try:
                event_data = json.loads(raw) if raw else {}
                if not isinstance(event_data, dict):
                    raise ValueError("not an object")
            except (ValueError, TypeError):
                errors["event_data"] = "invalid_json"
            else:
                self._captures = [
                    {
                        "trigger": "event",
                        "event_type": user_input["event_type"],
                        "event_data": event_data,
                    }
                ]
                self._accept_captures()
                return await self._async_advance()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required("event_type", default="zha_event"): (
                        selector.TextSelector()
                    ),
                    vol.Optional("event_data", default=""): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"button": self._button_label},
        )

    # --- advancing through the buttons -------------------------------------

    async def _async_advance(self, *, skip: bool = False) -> SubentryFlowResult:
        """Store the finished button and move to the next, or to timing."""
        if not skip and self._sources:
            self._buttons[ulid_now()] = {
                CONF_NAME: self._button_label,
                CONF_SOURCES: self._sources,
            }
        self._sources = []
        self._index += 1

        if self._index < self._button_count:
            return await self.async_step_learn()
        return await self.async_step_timing()

    # --- step 4: timing ----------------------------------------------------

    async def async_step_timing(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask once about multi-press, then create the remote."""
        if user_input is not None:
            self._multi_press_max = int(user_input[CONF_MULTI_PRESS_MAX])
            return self._create()

        return self.async_show_form(
            step_id="timing",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MULTI_PRESS_MAX, default=1
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value="1", label="Off - single presses stay instant"
                                ),
                                selector.SelectOptionDict(
                                    value="2", label="Up to double press"
                                ),
                                selector.SelectOptionDict(
                                    value="3", label="Up to triple press"
                                ),
                                selector.SelectOptionDict(
                                    value="4", label="Up to quadruple press"
                                ),
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={"remote": self._name},
        )

    @callback
    def _create(self) -> SubentryFlowResult:
        """Write the subentry."""
        if not self._buttons:
            return self.async_abort(reason="no_buttons")

        for button in self._buttons.values():
            button[CONF_MULTI_PRESS_MAX] = self._multi_press_max
            # Only meaningful when an edge source can measure duration; the
            # engine ignores it otherwise, and derive_event_types will not
            # declare long-press types for a button that cannot produce them.
            if any(src[CONF_SHAPE] == SHAPE_EDGE for src in button[CONF_SOURCES]):
                button[CONF_LONG_PRESS_THRESHOLD] = 0.5

        return self.async_create_entry(
            title=self._name,
            data={CONF_BUTTONS: self._buttons},
            unique_id=slugify(self._name),
        )
