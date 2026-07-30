"""Diagnostics for Virtual Remote.

Answers the question this integration will actually generate support requests
about: "which gestures can this button emit, and what is it listening to?"
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_BUTTONS, CONF_SHAPE
from .model import ButtonConfig, derive_event_types

# Home Assistant's own device and entity ids are not secrets, and they are the
# whole point of a trigger dump when a button stops matching. The Zigbee
# hardware address is an actual device identifier, so it goes.
TO_REDACT = {"device_ieee"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the config entry."""
    return {
        "remotes": [
            _remote_diagnostics(subentry.title, subentry.data)
            for subentry in entry.subentries.values()
        ]
    }


def _remote_diagnostics(title: str, data: dict[str, Any]) -> dict[str, Any]:
    buttons: list[dict[str, Any]] = []

    for button_id, raw in (data.get(CONF_BUTTONS) or {}).items():
        entry: dict[str, Any] = {"id": button_id}
        try:
            config = ButtonConfig.from_dict(raw)
        except (KeyError, ValueError) as err:
            # A button that fails to load is exactly what someone would be
            # filing a report about, so say so rather than omitting it.
            entry["error"] = str(err)
            entry["raw"] = async_redact_data(raw, TO_REDACT)
        else:
            entry.update(
                {
                    "name": config.name,
                    "shapes": sorted(config.shapes),
                    "event_types": derive_event_types(config),
                    "gestures": asdict(config.gesture_config),
                    "sources": [
                        async_redact_data(dict(source), TO_REDACT)
                        for source in raw.get("sources", [])
                    ],
                }
            )
        buttons.append(entry)

    return {"title": title, "button_count": len(buttons), "buttons": buttons}
