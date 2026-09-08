"""Use Home Assistant's real config-entry and native entity lifecycle."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evodnik.config_flow import schema
from custom_components.evodnik.diagnostics import async_get_config_entry_diagnostics
from custom_components.evodnik.valve import EvodnikValve

from .simulators import Device, eventually


@pytest.fixture
def entry_data(unused_tcp_port_factory):
    return {
        "target_host": "127.0.0.1",
        "target_port": unused_tcp_port_factory(),
        "listen_host": "127.0.0.1",
        "listen_port": unused_tcp_port_factory(),
        "mqtt_enabled": False,
        "mqtt_prefix": "synthetic/evodnik",
    }


@pytest.fixture
async def loaded_entry(hass, entry_data):
    entry = MockConfigEntry(domain="evodnik", title="eVodnik", data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_config_flow_accepts_offline_endpoints(hass, entry_data):
    with patch("custom_components.evodnik.async_setup_entry", new=AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_init("evodnik", context={"source": "user"})
        assert result["type"] == "form"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], entry_data)
        assert result["type"] == "create_entry"
        assert result["data"] == entry_data
        await hass.async_block_till_done()


async def test_config_flow_rejects_listener_conflict(hass, entry_data):
    server = await asyncio.start_server(
        lambda _reader, writer: writer.close(), "127.0.0.1", entry_data["listen_port"]
    )
    try:
        result = await hass.config_entries.flow.async_init(
            "evodnik", context={"source": "user"}, data=entry_data
        )
        assert result["errors"] == {"base": "listener_in_use"}
    finally:
        server.close()
        await server.wait_closed()


async def test_duplicate_entries_and_mqtt_prefix(hass, entry_data, unused_tcp_port_factory):
    entry_data["mqtt_enabled"] = True
    entry = MockConfigEntry(domain="evodnik", data=entry_data)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "evodnik", context={"source": "user"}, data=entry_data
    )
    assert result["errors"] == {"base": "listener_in_use"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**entry_data, "listen_port": unused_tcp_port_factory()}
    )
    assert result["errors"] == {"base": "mqtt_prefix_in_use"}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("listen_port", -1),
        ("target_port", 65536),
    ],
)
def test_invalid_inputs(entry_data, key, value):
    with pytest.raises(vol.Invalid):
        schema({})({**entry_data, key: value})


async def test_setup_offline_and_native_controls(hass, loaded_entry):
    runtime = loaded_entry.runtime_data
    assert hass.states.get("valve.evodnik_water").state == "unavailable"
    assert hass.states.get("binary_sensor.evodnik_device_connected").state == "off"
    assert hass.states.get("binary_sensor.evodnik_cloud_connected").state == "off"
    device = await Device.connect(runtime.proxy.bound_port)
    try:
        await eventually(lambda: hass.states.get("valve.evodnik_water").state == "open")
        await hass.services.async_call(
            "valve", "close_valve", {"entity_id": "valve.evodnik_water"}, blocking=True
        )
        assert hass.states.get("valve.evodnik_water").state == "closed"
        assert hass.states.get("sensor.evodnik_water_total").state == "42"
        assert hass.states.get("sensor.evodnik_last_response").state != "unknown"
        await hass.services.async_call(
            "valve", "open_valve", {"entity_id": "valve.evodnik_water"}, blocking=True
        )
        assert hass.states.get("valve.evodnik_water").state == "open"
        diagnostic = await async_get_config_entry_diagnostics(hass, loaded_entry)
        assert diagnostic["device_connected"]
        assert "target_host" not in str(diagnostic)
        assert "water_total_liters" not in diagnostic
        assert "valve" not in diagnostic
    finally:
        await device.close()
    await eventually(lambda: hass.states.get("valve.evodnik_water").state == "unavailable")
    with pytest.raises(HomeAssistantError):
        await EvodnikValve(loaded_entry, "water").async_close_valve()


async def test_stale_reports_expire_without_guessing_rollover(hass, loaded_entry):
    runtime = loaded_entry.runtime_data
    now = datetime.now(UTC)
    runtime.proxy._update(
        device_connected=True, valve="open", valve_updated=now, total_liters=42, counter_updated=now
    )
    assert hass.states.get("valve.evodnik_water").state == "open"
    assert hass.states.get("sensor.evodnik_water_total").attributes.get("state_class") is None
    old = now - timedelta(seconds=601)
    runtime.proxy._update(valve_updated=old, counter_updated=old)
    assert hass.states.get("valve.evodnik_water").state == "unknown"
    assert hass.states.get("sensor.evodnik_water_total").state == "unavailable"


async def test_reconfigure_releases_old_listener(hass, loaded_entry, unused_tcp_port_factory):
    old_port = loaded_entry.runtime_data.proxy.bound_port
    new_port = unused_tcp_port_factory()
    result = await hass.config_entries.flow.async_init(
        "evodnik",
        context={
            "source": "reconfigure",
            "entry_id": loaded_entry.entry_id,
        },
        data={**loaded_entry.data, "listen_port": new_port},
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert loaded_entry.runtime_data.proxy.bound_port == new_port
    server = await asyncio.start_server(
        lambda _reader, writer: writer.close(), "127.0.0.1", old_port
    )
    server.close()
    await server.wait_closed()


async def test_bind_race_retries_cleanly(hass, entry_data):
    server = await asyncio.start_server(
        lambda _reader, writer: writer.close(), "127.0.0.1", entry_data["listen_port"]
    )
    entry = MockConfigEntry(domain="evodnik", title="eVodnik", data=entry_data)
    entry.add_to_hass(hass)
    try:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
    finally:
        server.close()
        await server.wait_closed()
        await hass.config_entries.async_unload(entry.entry_id)


async def test_shutdown_releases_active_connections(hass, loaded_entry):
    runtime = loaded_entry.runtime_data
    device = await Device.connect(runtime.proxy.bound_port)
    try:
        await eventually(lambda: runtime.proxy.state.device_connected)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert runtime.proxy.bound_port is None
        assert not runtime.proxy._tasks
    finally:
        await device.close()


async def test_mqtt_enabled_but_not_configured_still_starts(hass, entry_data):
    entry = MockConfigEntry(
        domain="evodnik", title="eVodnik", data={**entry_data, "mqtt_enabled": True}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    try:
        assert entry.runtime_data.proxy.bound_port == entry_data["listen_port"]
        assert entry.runtime_data.publisher is not None
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_without_listener_change(hass, loaded_entry, unused_tcp_port_factory):
    old_runtime = loaded_entry.runtime_data
    old_port = old_runtime.proxy.bound_port
    result = await hass.config_entries.flow.async_init(
        "evodnik",
        context={
            "source": "reconfigure",
            "entry_id": loaded_entry.entry_id,
        },
        data={**loaded_entry.data, "target_port": unused_tcp_port_factory()},
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert loaded_entry.runtime_data is not old_runtime
    assert loaded_entry.runtime_data.proxy.bound_port == old_port
    assert old_runtime.proxy.bound_port is None


async def test_direct_proxy_loop_is_rejected(hass, entry_data):
    result = await hass.config_entries.flow.async_init(
        "evodnik",
        context={"source": "user"},
        data={
            **entry_data,
            "target_port": entry_data["listen_port"],
        },
    )
    assert result["errors"] == {"base": "proxy_loop"}


async def set_meter_in_ui(hass, entry, reading):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"meter_reading": reading}
    )


@pytest.mark.parametrize("initial", [0, 12000])
async def test_meter_configuration_and_increment(hass, loaded_entry, initial):
    runtime = loaded_entry.runtime_data
    assert hass.states.get("sensor.evodnik_water_meter").state == "unavailable"
    device = await Device.connect(runtime.proxy.bound_port)
    try:
        await eventually(lambda: runtime.total_liters == 42)
        session = runtime.proxy._session
        # Calibration must read the new counter instead of using the cached value.
        device.counter = 43
        result = await set_meter_in_ui(hass, loaded_entry, initial)
        assert result["type"] == "create_entry"
        assert runtime.meter_liters == initial
        assert runtime.total_liters == 43
        device.counter = 50
        await runtime.proxy.refresh()
        await runtime.proxy.refresh()
        assert runtime.meter_liters == initial + 7
        state = hass.states.get("sensor.evodnik_water_meter")
        assert state.state == str(initial + 7)
        assert state.attributes["calibration_status"] == "ready"
        assert state.attributes["unit_of_measurement"] == "L"
        assert runtime.proxy._session is session
        assert all(frame.opcode == 0x31 for frame in device.requests)
        diagnostic = await async_get_config_entry_diagnostics(hass, loaded_entry)
        assert diagnostic["meter_calibration_status"] == "ready"
        assert "offset" not in diagnostic
        assert "last_raw" not in diagnostic
        assert "water_meter_liters" not in diagnostic
    finally:
        await device.close()


async def test_meter_survives_reload_and_counts_disconnected_usage(hass, loaded_entry):
    runtime = loaded_entry.runtime_data
    first = await Device.connect(runtime.proxy.bound_port)
    try:
        await eventually(lambda: runtime.total_liters == 42)
        await set_meter_in_ui(hass, loaded_entry, 12000)
        first.counter = 49
        await runtime.proxy.refresh()
        assert runtime.meter_liters == 12007
        assert await hass.config_entries.async_reload(loaded_entry.entry_id)
        runtime = loaded_entry.runtime_data
        assert runtime.meter_liters is None
        assert runtime.meter.status == "ready"
        assert runtime.meter.last_raw == 49
        second = await Device.connect(runtime.proxy.bound_port)
        second.counter = 53
        try:
            await eventually(lambda: runtime.meter_liters == 12011)
            assert hass.states.get("sensor.evodnik_water_meter").state == "12011"
        finally:
            await second.close()
    finally:
        await first.close()


async def test_counter_decrease_requires_recalibration_even_after_reload(hass, loaded_entry):
    runtime = loaded_entry.runtime_data
    first = await Device.connect(runtime.proxy.bound_port)
    try:
        await eventually(lambda: runtime.total_liters == 42)
        await set_meter_in_ui(hass, loaded_entry, 12000)
        first.counter = 49
        await runtime.proxy.refresh()
        # This is above the original baseline but below the most recent counter.
        first.counter = 45
        await runtime.proxy.refresh()
        assert runtime.meter_liters is None
        assert runtime.total_liters == 45
        assert runtime.meter.status == "counter_reset"
        assert await hass.config_entries.async_reload(loaded_entry.entry_id)
        runtime = loaded_entry.runtime_data
        second = await Device.connect(runtime.proxy.bound_port)
        second.counter = 60
        try:
            await eventually(lambda: runtime.total_liters == 60)
            state = hass.states.get("sensor.evodnik_water_meter")
            assert state.state == "unknown"
            assert state.attributes["calibration_status"] == "counter_reset"
            await set_meter_in_ui(hass, loaded_entry, 0)
            second.counter = 67
            await runtime.proxy.refresh()
            assert runtime.meter_liters == 7
            assert runtime.meter.status == "ready"
        finally:
            await second.close()
    finally:
        await first.close()


async def test_meter_offline_calibration_fails_without_changing_offset(hass, loaded_entry):
    runtime = loaded_entry.runtime_data
    device = await Device.connect(runtime.proxy.bound_port)
    try:
        await eventually(lambda: runtime.total_liters == 42)
        await set_meter_in_ui(hass, loaded_entry, 12000)
    finally:
        await device.close()
    await eventually(lambda: not runtime.proxy.state.device_connected)
    result = await set_meter_in_ui(hass, loaded_entry, 0)
    assert result["errors"] == {"base": "counter_unavailable"}
    assert runtime.meter.offset == 12000 - 42
    assert runtime.meter_liters is None


async def test_meter_calibration_requires_a_new_valid_response(hass, loaded_entry):
    from dataclasses import replace

    runtime = loaded_entry.runtime_data
    runtime.proxy.settings = replace(runtime.proxy.settings, request_timeout=0.05)
    device = await Device.connect(runtime.proxy.bound_port)
    try:
        await eventually(lambda: runtime.total_liters == 42)
        await set_meter_in_ui(hass, loaded_entry, 12000)
        device.respond = False
        result = await set_meter_in_ui(hass, loaded_entry, 0)
        assert result["errors"] == {"base": "counter_unavailable"}
        assert runtime.meter.offset == 12000 - 42
    finally:
        await device.close()


@pytest.mark.parametrize("reading", [1.5, float("nan")])
async def test_meter_rejects_fractional_and_nonfinite_input(hass, loaded_entry, reading):
    result = await set_meter_in_ui(hass, loaded_entry, reading)
    assert result["errors"] == {"meter_reading": "invalid_reading"}
    assert loaded_entry.runtime_data.meter.offset is None


async def test_meter_options_require_running_integration(hass, entry_data):
    entry = MockConfigEntry(domain="evodnik", data=entry_data)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["reason"] == "integration_unavailable"


async def test_removing_entry_removes_meter_storage(hass, loaded_entry):
    from custom_components.evodnik.meter import meter_store

    runtime = loaded_entry.runtime_data
    device = await Device.connect(runtime.proxy.bound_port)
    try:
        await eventually(lambda: runtime.total_liters == 42)
        await set_meter_in_ui(hass, loaded_entry, 12000)
        assert await meter_store(hass, loaded_entry.entry_id).async_load() is not None
        assert await hass.config_entries.async_remove(loaded_entry.entry_id)
        assert await meter_store(hass, loaded_entry.entry_id).async_load() is None
    finally:
        await device.close()
