"""Track published topics and retry retained discovery removal after outages."""

import asyncio

from homeassistant.components import mqtt
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

DATA_REGISTRY = "evodnik_mqtt_discovery"


def connected(hass: HomeAssistant) -> bool:
    """MQTT may be absent, disabled, or temporarily disconnected."""
    try:
        return mqtt.is_connected(hass)
    except KeyError:
        return False


async def async_get_registry(hass: HomeAssistant) -> DiscoveryRegistry:
    """Share cleanup across reloads, including entries with MQTT turned off."""
    if DATA_REGISTRY not in hass.data:
        hass.data[DATA_REGISTRY] = DiscoveryRegistry(hass)
    registry = hass.data[DATA_REGISTRY]
    await registry.load()
    return registry


class DiscoveryRegistry:
    """Persist topic ownership before publication; never store device identities."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store = Store(hass, 1, DATA_REGISTRY, private=True, atomic_writes=True)
        self.entries: dict[str, list[str]] = {}
        self.pending: set[str] = set()
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        self._loaded = False
        self._task: asyncio.Task | None = None
        self._unsubscribe = None

    async def load(self) -> None:
        async with self._lock:
            if self._loaded:
                return
            data = await self.store.async_load() or {}
            self.entries = data.get("entries", {})
            self.pending = set(data.get("pending", []))
            # A removal may have happened immediately before HA stopped.
            for entry_id in list(self.entries):
                entry = self.hass.config_entries.async_get_entry(entry_id)
                if entry is None or not entry.data.get("mqtt_enabled", False):
                    self.pending.update(self.entries.pop(entry_id))
            self._loaded = True
            self._event.set()
            self._unsubscribe = mqtt.async_subscribe_connection_status(
                self.hass, self._connection_changed
            )
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.stop)
            self._task = self.hass.async_create_background_task(
                self._run(), "evodnik MQTT discovery cleanup"
            )

    @callback
    def _connection_changed(self, _connected: bool) -> None:
        self._event.set()

    async def _save(self) -> None:
        await self.store.async_save({"entries": self.entries, "pending": sorted(self.pending)})

    async def set_topics(self, entry_id: str, topics: list[str]) -> None:
        """Keep stable discovery topics; retire old state topics on prefix changes."""
        async with self._lock:
            previous = self.entries.get(entry_id, [])
            if previous == topics:
                return
            self.pending.update(set(previous) - set(topics))
            self.pending.difference_update(topics)
            if topics:
                self.entries[entry_id] = topics
            else:
                self.entries.pop(entry_id, None)
            await self._save()
        self._event.set()
        await self.flush()

    async def flush(self) -> None:
        """An empty retained payload removes discovery and obsolete state."""
        async with self._lock:
            try:
                async with asyncio.timeout(5):
                    for topic in sorted(self.pending):
                        if not connected(self.hass):
                            break
                        await mqtt.async_publish(self.hass, topic, "", qos=1, retain=True)
                        self.pending.remove(topic)
                        await self._save()
            except HomeAssistantError, OSError, TimeoutError, KeyError:
                # Keep unfinished removals for the next connection or retry.
                pass

    async def _run(self) -> None:
        while True:
            try:
                async with asyncio.timeout(15):
                    await self._event.wait()
            except TimeoutError:
                pass
            self._event.clear()
            await self.flush()

    async def stop(self, _event=None) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
