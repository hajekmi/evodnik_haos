"""Direct local water valve control with confirmed protocol readback."""

from homeassistant.components.valve import ValveDeviceClass, ValveEntity, ValveEntityFeature
from homeassistant.exceptions import HomeAssistantError

from .entity import EvodnikEntity
from .proxy import CommandNotConfirmed, DeviceUnavailable, QueueFull


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([EvodnikValve(entry, "water")])


class EvodnikValve(EvodnikEntity, ValveEntity):
    _attr_device_class = ValveDeviceClass.WATER
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
    _attr_reports_position = False

    @property
    def available(self) -> bool:
        return self.runtime.proxy.state.device_connected

    @property
    def is_closed(self) -> bool | None:
        state = self.runtime.valve
        return state == "closed" if state is not None else None

    async def _set(self, state: str) -> None:
        try:
            await self.runtime.proxy.set_valve(state)
        except (DeviceUnavailable, QueueFull, CommandNotConfirmed) as error:
            raise HomeAssistantError(str(error)) from None

    async def async_open_valve(self) -> None:
        await self._set("open")

    async def async_close_valve(self) -> None:
        await self._set("closed")
