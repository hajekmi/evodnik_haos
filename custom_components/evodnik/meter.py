"""A user-set meter reading derived from the device's cumulative counter."""

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN


def meter_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, 1, f"{DOMAIN}.{entry_id}.meter", private=True, atomic_writes=True)


class Meter:
    """Persist an offset and the last counter; never count received messages."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.store = meter_store(hass, entry_id)
        self.offset: int | None = None
        self.last_raw: int | None = None
        self.reset_detected = False

    @property
    def status(self) -> str:
        if self.reset_detected:
            return "counter_reset"
        return "ready" if self.offset is not None else "not_configured"

    def _data(self) -> dict:
        return {
            "offset": self.offset,
            "last_raw": self.last_raw,
            "reset_detected": self.reset_detected,
        }

    async def load(self) -> None:
        if data := await self.store.async_load():
            self.offset = data["offset"]
            self.last_raw = data["last_raw"]
            self.reset_detected = data["reset_detected"]

    async def save(self) -> None:
        await self.store.async_save(self._data())

    @callback
    def observe(self, raw: int | None) -> None:
        if raw is None or self.offset is None or raw == self.last_raw:
            return
        if self.last_raw is not None and raw < self.last_raw:
            self.offset = None
            self.reset_detected = True
        self.last_raw = raw
        self.store.async_delay_save(self._data, 1)

    @callback
    def set_reading(self, reading: int, raw: int) -> None:
        self.offset = reading - raw
        self.last_raw = raw
        self.reset_detected = False

    def reading(self, raw: int | None) -> int | None:
        if raw is None or self.offset is None:
            return None
        return raw + self.offset
