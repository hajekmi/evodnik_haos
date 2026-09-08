"""Optional, bounded state publication through Home Assistant's MQTT client."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import VALVE_MAX_AGE
from .proxy import DeviceUnavailable, QueueFull
from .runtime import Runtime


class Publisher:
    """Mirror reports only; no command subscription or second MQTT connection."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime, prefix: str) -> None:
        self.hass = hass
        self.runtime = runtime
        self.prefix = prefix
        self._event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._unsubscribers = []
        self._connected_at: datetime | None = None
        self._refresh_needed = False

    def _connected(self) -> bool:
        try:
            return mqtt.is_connected(self.hass)
        except KeyError:
            return False

    @callback
    def _connection_changed(self, connected: bool) -> None:
        self._connected_at = datetime.now(UTC) if connected else None
        self._refresh_needed = connected
        self._event.set()

    @callback
    def start(self) -> None:
        self._unsubscribers = [
            mqtt.async_subscribe_connection_status(self.hass, self._connection_changed),
            self.runtime.subscribe(self._event.set),
        ]
        self._connection_changed(self._connected())
        self._task = self.hass.async_create_background_task(self._run(), "evodnik MQTT")

    def _payload(self, *, offline: bool = False) -> dict:
        state = self.runtime.proxy.state
        observed = state.valve_updated
        verified = (
            not offline
            and self._connected_at is not None
            and observed is not None
            and observed >= self._connected_at
            and self.runtime.valve is not None
        )
        return {
            "available": bool(verified),
            "valve": self.runtime.valve if verified else "unknown",
            "observed_at": observed.isoformat() if verified else None,
            "valid_until": (observed + timedelta(seconds=VALVE_MAX_AGE)).isoformat()
            if verified
            else None,
            "water_total_liters": self.runtime.total_liters if verified else None,
            "water_meter_liters": self.runtime.meter_liters if verified else None,
            "meter_calibration_status": self.runtime.meter.status,
            "counter_observed_at": state.counter_updated.isoformat()
            if verified and state.counter_updated
            else None,
            "device_connected": state.device_connected and not offline,
            "cloud_connected": state.cloud_connected and not offline,
        }

    async def _publish(self, *, offline: bool = False) -> None:
        payload = self._payload(offline=offline)
        async with asyncio.timeout(5):
            # Offline first invalidates any retained report from an earlier session.
            if not payload["available"]:
                await mqtt.async_publish(
                    self.hass, f"{self.prefix}/availability", "offline", qos=1, retain=True
                )
            await mqtt.async_publish(
                self.hass, f"{self.prefix}/state", json.dumps(payload), qos=1, retain=True
            )
            if payload["available"]:
                await mqtt.async_publish(
                    self.hass, f"{self.prefix}/availability", "online", qos=1, retain=True
                )

    async def _run(self) -> None:
        while True:
            try:
                async with asyncio.timeout(15):
                    await self._event.wait()
            except TimeoutError:
                pass
            self._event.clear()
            if not self._connected():
                self._connected_at = None
                continue
            if self._connected_at is None:
                self._connection_changed(True)
            try:
                await self._publish()
                if self._refresh_needed:
                    self._refresh_needed = False
                    async with asyncio.timeout(15):
                        await self.runtime.proxy.refresh()
                    self._event.set()
            except (
                HomeAssistantError,
                OSError,
                TimeoutError,
                DeviceUnavailable,
                QueueFull,
                KeyError,
            ):
                # A broker or device outage must never interrupt native controls.
                continue

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._connected():
            try:
                await self._publish(offline=True)
            except HomeAssistantError, OSError, TimeoutError, KeyError:
                pass
