"""The Virtual Remote integration.

One config entry holds the integration; each virtual remote is a subentry, and
each subentry becomes a device with one `event` entity per button.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [Platform.EVENT]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Virtual Remote from a config entry."""
    # Covers subentry reconfigure too: async_update_subentry notifies the parent
    # entry's update listeners.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on any change to the entry or its subentries.

    A full reload rather than mutating entities in place: trigger subscriptions
    have to be re-attached whenever a trigger config changes, and pending timers
    plus in-flight engine state must be discarded rather than carried across a
    configuration change.
    """
    hass.config_entries.async_schedule_reload(entry.entry_id)
