"""Persistent offset behavior using HA storage and synthetic counters."""

from custom_components.evodnik.meter import Meter


async def test_meter_storage_is_independent_per_entry(hass):
    first = Meter(hass, "first-device")
    second = Meter(hass, "second-device")
    first.set_reading(12000, 42)
    second.set_reading(0, 100)
    await first.save()
    await second.save()
    restored = Meter(hass, "first-device")
    await restored.load()
    assert restored.reading(49) == 12007
    await second.load()
    assert second.reading(107) == 7


async def test_reset_while_offline_is_detected_from_saved_counter(hass):
    meter = Meter(hass, "offline-reset")
    meter.set_reading(12000, 42)
    meter.observe(50)
    await meter.save()
    restored = Meter(hass, "offline-reset")
    await restored.load()
    restored.observe(3)
    assert restored.reading(3) is None
    assert restored.status == "counter_reset"
    await restored.save()
