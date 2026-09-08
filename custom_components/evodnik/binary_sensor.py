"""Separate device and vendor connection diagnostics."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import EvodnikEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(
        [
            ConnectionSensor(entry, "device_connected"),
            ConnectionSensor(entry, "cloud_connected"),
        ]
    )


class ConnectionSensor(EvodnikEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry, key: str) -> None:
        super().__init__(entry, key)
        self.key = key

    @property
    def is_on(self) -> bool:
        return getattr(self.runtime.proxy.state, self.key)
