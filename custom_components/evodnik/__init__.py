"""eVodnik local control and vendor TCP proxy."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_LISTEN_HOST,
    CONF_LISTEN_PORT,
    CONF_MQTT_ENABLED,
    CONF_MQTT_PREFIX,
    CONF_TARGET_HOST,
    CONF_TARGET_PORT,
)
from .meter import meter_store
from .proxy import Settings
from .runtime import Runtime

PLATFORMS = (Platform.VALVE, Platform.SENSOR, Platform.BINARY_SENSOR)
type EvodnikConfigEntry = ConfigEntry[Runtime]


async def async_setup_entry(hass: HomeAssistant, entry: EvodnikConfigEntry) -> bool:
    """Start the listener without requiring an online vendor, device, or broker."""
    data = entry.data
    runtime = entry.runtime_data = Runtime(
        hass,
        Settings(
            target_host=data[CONF_TARGET_HOST],
            target_port=data[CONF_TARGET_PORT],
            listen_port=data[CONF_LISTEN_PORT],
            listen_host=data[CONF_LISTEN_HOST],
        ),
        entry.entry_id,
    )
    try:
        await runtime.start()
    except OSError:
        raise ConfigEntryNotReady("Cannot bind the configured listener") from None
    try:
        if data[CONF_MQTT_ENABLED]:
            from .mqtt import Publisher

            runtime.publisher = Publisher(hass, runtime, data[CONF_MQTT_PREFIX])
            runtime.publisher.start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await runtime.stop()
        raise

    async def stop(_event) -> None:
        await runtime.stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EvodnikConfigEntry) -> bool:
    """Release all sockets, waiters, subscriptions, and timers."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.stop()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: EvodnikConfigEntry) -> None:
    """Remove this entry's local meter calibration when the integration is deleted."""
    await meter_store(hass, entry.entry_id).async_remove()
