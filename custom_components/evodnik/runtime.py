"""Shared state, freshness, and integration lifecycle."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import COUNTER_MAX_AGE, VALVE_MAX_AGE
from .meter import Meter
from .proxy import DeviceUnavailable, Proxy, Settings


class Runtime:
    """Provide one state store to native entities and the optional MQTT mirror."""

    def __init__(self, hass: HomeAssistant, settings: Settings, entry_id: str) -> None:
        self.hass = hass
        self.listeners: set[Callable[[], None]] = set()
        self.meter = Meter(hass, entry_id)
        self.proxy = Proxy(settings, self._proxy_changed)
        self._calibration_lock = asyncio.Lock()
        self._running = False
        self.publisher = None
        self._cancel_timer: Callable[[], None] | None = None

    @callback
    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    @callback
    def notify(self, _now: datetime | None = None) -> None:
        for listener in tuple(self.listeners):
            listener()

    @callback
    def _proxy_changed(self) -> None:
        self.meter.observe(self.total_liters)
        self.notify()

    def fresh(self, observed: datetime | None, max_age: int) -> bool:
        return (
            self.proxy.state.device_connected
            and observed is not None
            and 0 <= (datetime.now(UTC) - observed).total_seconds() <= max_age
        )

    @property
    def valve(self) -> str | None:
        state = self.proxy.state
        return state.valve if self.fresh(state.valve_updated, VALVE_MAX_AGE) else None

    @property
    def total_liters(self) -> int | None:
        state = self.proxy.state
        return state.total_liters if self.fresh(state.counter_updated, COUNTER_MAX_AGE) else None

    @property
    def meter_liters(self) -> int | None:
        return self.meter.reading(self.total_liters)

    async def set_meter_reading(self, reading: int) -> None:
        """Align the user's reading with a fresh counter without writing to the device."""
        if type(reading) is not int or reading < 0:
            raise ValueError("Enter a nonnegative whole number of liters")
        async with self._calibration_lock:
            if not self._running:
                raise DeviceUnavailable("Device unavailable")
            async with asyncio.timeout(15):
                await self.proxy.refresh()
            raw = self.total_liters
            if not self._running or raw is None:
                raise DeviceUnavailable("Device unavailable")
            self.meter.set_reading(reading, raw)
            self.notify()
            await self.meter.save()

    async def start(self) -> None:
        await self.meter.load()
        await self.proxy.start()
        self._running = True
        self._cancel_timer = async_track_time_interval(
            self.hass, self.notify, timedelta(seconds=15)
        )

    async def stop(self) -> None:
        self._running = False
        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None
        await self.proxy.stop()
        if self.publisher:
            await self.publisher.stop()
            self.publisher = None
        async with self._calibration_lock:
            await self.meter.save()
