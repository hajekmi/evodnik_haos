"""Reported water counter and response timestamp."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfVolume
from homeassistant.helpers.entity import EntityCategory

from .entity import EvodnikEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(
        [
            WaterTotal(entry, "water_total"),
            WaterMeter(entry, "water_meter"),
            LastResponse(entry, "last_response"),
        ]
    )


class WaterTotal(EvodnikEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    # No total_increasing claim: rollover/reset and the upper counter bits are unverified.

    @property
    def available(self) -> bool:
        return self.runtime.total_liters is not None

    @property
    def native_value(self) -> int | None:
        return self.runtime.total_liters

    @property
    def extra_state_attributes(self) -> dict:
        observed = self.runtime.proxy.state.counter_updated
        return {"observed_at": observed.isoformat() if observed else None}


class LastResponse(EvodnikEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return self.runtime.proxy.state.last_response


class WaterMeter(WaterTotal):
    """User-aligned reading; a decreasing device counter requires recalibration."""

    @property
    def native_value(self) -> int | None:
        return self.runtime.meter_liters

    @property
    def extra_state_attributes(self) -> dict:
        return {**super().extra_state_attributes, "calibration_status": self.runtime.meter.status}
