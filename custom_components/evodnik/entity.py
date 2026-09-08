"""Push-based entities backed by the proxy's shared state."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class EvodnikEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry, key: str) -> None:
        self.runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="eVodnik",
            model="TCP water controller",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.runtime.subscribe(self.async_write_ha_state))
