"""MQTT discovery, verified telemetry, and bounded local valve commands."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from homeassistant.components import mqtt
from homeassistant.components.mqtt.const import DEFAULT_BIRTH, DEFAULT_PREFIX, DEFAULT_WILL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import VALVE_MAX_AGE
from .mqtt_discovery import async_get_registry, connected
from .proxy import CommandNotConfirmed, DeviceUnavailable, QueueFull
from .runtime import Runtime

_LOGGER = logging.getLogger(__name__)


class Publisher:
    """Use HA's MQTT connection and the proxy's existing transaction queue."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime, prefix: str) -> None:
        self.hass = hass
        self.runtime = runtime
        self.prefix = prefix
        self._event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._unsubscribers = []
        self._connected_at: datetime | None = None
        self._refresh_needed = False
        self._discovery_needed = True
        self._message_unsubscribers = []
        self._subscription_settings: dict | None = None
        self._command_task: asyncio.Task | None = None
        self._stopping = False
        self._generation = 0

    def _connected(self) -> bool:
        return connected(self.hass)

    def _mqtt_settings(self) -> dict:
        entries = self.hass.config_entries.async_entries(mqtt.DOMAIN)
        return dict(entries[0].data | entries[0].options) if entries else {}

    @callback
    def _connection_changed(self, connected: bool) -> None:
        self._generation += 1
        self._connected_at = datetime.now(UTC) if connected else None
        self._refresh_needed = connected
        self._discovery_needed = True
        if not connected and self._command_task:
            self._command_task.cancel()
        self._event.set()

    @callback
    def _birth_received(self, message: mqtt.ReceiveMessage) -> None:
        birth = self._mqtt_settings().get(mqtt.CONF_BIRTH_MESSAGE, DEFAULT_BIRTH)
        if not message.retain and message.payload == birth.get("payload"):
            self._connection_changed(True)

    @callback
    def _command_received(self, message: mqtt.ReceiveMessage) -> None:
        # One MQTT request at a time bounds work even under a message burst.
        if (
            self._stopping
            or message.retain
            or message.payload not in ("OPEN", "CLOSE")
            or not self._connected()
            or not self._payload()["available"]
            or (self._command_task and not self._command_task.done())
        ):
            return
        self._command_task = self.hass.async_create_background_task(
            self._set_valve("open" if message.payload == "OPEN" else "closed"),
            "evodnik MQTT valve command",
        )

    async def _set_valve(self, state: str) -> None:
        try:
            # Eager task execution binds this request to the current TCP session.
            async with asyncio.timeout(15):
                await self.runtime.proxy.set_valve(state)
        except DeviceUnavailable, QueueFull, CommandNotConfirmed, OSError, TimeoutError:
            _LOGGER.warning("MQTT valve command was not confirmed; it will not be retried")
        finally:
            self._event.set()

    async def _subscribe(self) -> None:
        settings = self._mqtt_settings()
        if self._subscription_settings == settings:
            return
        for unsubscribe in self._message_unsubscribers:
            unsubscribe()
        self._message_unsubscribers.clear()
        self._message_unsubscribers.append(
            await mqtt.async_subscribe(
                self.hass, f"{self.prefix}/valve/set", self._command_received, qos=0
            )
        )
        birth = settings.get(mqtt.CONF_BIRTH_MESSAGE, DEFAULT_BIRTH)
        if birth.get("topic") and birth.get("payload"):
            self._message_unsubscribers.append(
                await mqtt.async_subscribe(self.hass, birth["topic"], self._birth_received)
            )
        self._subscription_settings = settings

    def _availability(self, topic: str) -> dict:
        availability = [{"topic": topic}]
        will = self._mqtt_settings().get(mqtt.CONF_WILL_MESSAGE, DEFAULT_WILL)
        if will.get("topic") and will.get("payload"):
            # Only a fresh proxy report may restore availability. HA's birth alone
            # must not enable a valve, and the birth message need not be retained.
            availability.append(
                {
                    "topic": will["topic"],
                    "value_template": "{{ 'offline' if value == "
                    + json.dumps(will["payload"])
                    + " else 'ignore' }}",
                }
            )
        return {"availability": availability, "availability_mode": "latest"}

    def _discovery(self) -> dict[str, dict]:
        identifier = f"evodnik_{self.runtime.entry_id}"
        entry = self.hass.config_entries.async_get_entry(self.runtime.entry_id)
        device = {
            "identifiers": [identifier],
            "name": f"{entry.title if entry else 'eVodnik'} MQTT",
            "manufacturer": "eVodnik",
            "model": "TCP water controller",
        }
        entities = [
            (
                "valve",
                "water",
                {
                    "name": "Water",
                    "device_class": "water",
                    "command_topic": f"{self.prefix}/valve/set",
                    "payload_open": "OPEN",
                    "payload_close": "CLOSE",
                    "payload_stop": None,
                    "state_open": "open",
                    "state_closed": "closed",
                    "value_template": "{{ value_json.valve }}",
                    "optimistic": False,
                    "retain": False,
                    "qos": 0,
                },
            ),
        ]
        for key, name in (("water_total", "Water total"), ("water_meter", "Water meter")):
            config = {
                "name": name,
                "device_class": "water",
                "unit_of_measurement": "L",
                "value_template": "{{ value_json." + key + "_liters }}",
                "expire_after": VALVE_MAX_AGE,
            }
            if key == "water_meter":
                config.update(
                    json_attributes_topic=f"{self.prefix}/state",
                    json_attributes_template="{{ {'calibration_status': "
                    "value_json.meter_calibration_status} | tojson }}",
                )
            entities.append(("sensor", key, config))
        for key, name in (
            ("device_connected", "Device connected"),
            ("cloud_connected", "Cloud connected"),
        ):
            entities.append(
                (
                    "binary_sensor",
                    key,
                    {
                        "name": name,
                        "device_class": "connectivity",
                        "entity_category": "diagnostic",
                        "value_template": "{{ 'ON' if value_json." + key + " else 'OFF' }}",
                        "expire_after": VALVE_MAX_AGE,
                    },
                )
            )
        discovery_prefix = self._mqtt_settings().get(mqtt.CONF_DISCOVERY_PREFIX, DEFAULT_PREFIX)
        result = {}
        for component, key, config in entities:
            availability = "bridge_availability" if component == "binary_sensor" else "availability"
            result[f"{discovery_prefix}/{component}/{identifier}/{key}/config"] = {
                "unique_id": f"{identifier}_mqtt_{key}",
                "device": device,
                "state_topic": f"{self.prefix}/state",
                **self._availability(f"{self.prefix}/{availability}"),
                **config,
            }
        return result

    async def _publish_discovery(self) -> None:
        generation = self._generation
        discovery = self._discovery()
        registry = await async_get_registry(self.hass)
        await registry.set_topics(
            self.runtime.entry_id,
            [
                *discovery,
                *(
                    f"{self.prefix}/{key}"
                    for key in ("state", "availability", "bridge_availability")
                ),
            ],
        )
        for topic, payload in discovery.items():
            if not self._connected():
                return
            async with asyncio.timeout(5):
                await mqtt.async_publish(self.hass, topic, json.dumps(payload), qos=1, retain=True)
        if generation == self._generation:
            self._discovery_needed = False

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
        generation = self._generation
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
            await mqtt.async_publish(
                self.hass,
                f"{self.prefix}/bridge_availability",
                "offline" if offline else "online",
                qos=1,
                retain=True,
            )
            if payload["available"]:
                available = generation == self._generation and self._payload()["available"]
                await mqtt.async_publish(
                    self.hass,
                    f"{self.prefix}/availability",
                    "online" if available else "offline",
                    qos=1,
                    retain=True,
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
                await self._subscribe()
                # Invalidate previous reports before exposing a command entity.
                await self._publish()
                if self._discovery_needed:
                    await self._publish_discovery()
                    self._event.set()
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
        self._stopping = True
        for unsubscribe in self._message_unsubscribers:
            unsubscribe()
        self._message_unsubscribers.clear()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self._command_task:
            self._command_task.cancel()
            await asyncio.gather(self._command_task, return_exceptions=True)
            self._command_task = None
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._connected():
            try:
                await self._publish(offline=True)
            except HomeAssistantError, OSError, TimeoutError, KeyError:
                pass
