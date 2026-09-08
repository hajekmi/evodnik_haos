"""MQTT uses HA's configured client and never feeds reports back into commands."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from homeassistant.components import mqtt
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .simulators import Device, eventually


@pytest.fixture(autouse=True)
def close_mock_mqtt_socket(mqtt_mock, mqtt_client_mock):
    """The mock Paho client does not emit its socket-close event at shutdown."""
    yield
    mqtt_client_mock.on_socket_close(mqtt_client_mock, None, Mock(fileno=lambda: -1))


def payloads(client, prefix):
    return [
        json.loads(call.args[1])
        for call in client.async_publish.call_args_list
        if call.args[0] == f"{prefix}/state"
    ]


async def test_mqtt_outage_reconnect_and_unload(hass, mqtt_mock, unused_tcp_port_factory):
    prefix = "synthetic/evodnik"
    entry = MockConfigEntry(
        domain="evodnik",
        title="eVodnik",
        data={
            "target_host": "127.0.0.1",
            "target_port": unused_tcp_port_factory(),
            "listen_host": "127.0.0.1",
            "listen_port": unused_tcp_port_factory(),
            "mqtt_enabled": True,
            "mqtt_prefix": prefix,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    runtime = entry.runtime_data
    device = await Device.connect(runtime.proxy.bound_port)
    try:
        await eventually(lambda: any(item["available"] for item in payloads(mqtt_mock, prefix)))
        assert payloads(mqtt_mock, prefix)[-1]["valve"] == "open"
        await runtime.set_meter_reading(12000)
        device.counter = 49
        await runtime.proxy.refresh()
        await eventually(lambda: payloads(mqtt_mock, prefix)[-1]["water_meter_liters"] == 12007)
        assert payloads(mqtt_mock, prefix)[-1]["meter_calibration_status"] == "ready"
        mqtt_mock.connected = False
        async_dispatcher_send(hass, mqtt.MQTT_CONNECTION_STATE, False)
        await hass.async_block_till_done()
        before = mqtt_mock.async_publish.call_count
        await runtime.proxy.set_valve("closed")
        assert hass.states.get("valve.evodnik_water").state == "closed"
        await hass.async_block_till_done()
        assert mqtt_mock.async_publish.call_count == before
        write_count = sum(bool(request.valve_command) for request in device.requests)
        mqtt_mock.connected = True
        mqtt_mock.async_publish.reset_mock()
        async_dispatcher_send(hass, mqtt.MQTT_CONNECTION_STATE, True)
        await eventually(lambda: any(item["available"] for item in payloads(mqtt_mock, prefix)))
        reports = payloads(mqtt_mock, prefix)
        assert reports[0]["available"] is False
        assert reports[-1]["valve"] == "closed"
        assert reports[-1]["water_meter_liters"] == 12007
        assert reports[-1]["valid_until"] is not None
        assert write_count == sum(bool(request.valve_command) for request in device.requests)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert payloads(mqtt_mock, prefix)[-1]["available"] is False
        assert payloads(mqtt_mock, prefix)[-1]["valve"] == "unknown"
        assert payloads(mqtt_mock, prefix)[-1]["water_meter_liters"] is None
        assert not runtime.listeners
    finally:
        await device.close()
        if runtime.proxy.bound_port is not None:
            await hass.config_entries.async_unload(entry.entry_id)


async def test_retained_report_requires_post_reconnect_device_evidence(
    hass, mqtt_mock, unused_tcp_port_factory
):
    from custom_components.evodnik.mqtt import Publisher
    from custom_components.evodnik.proxy import Settings
    from custom_components.evodnik.runtime import Runtime

    runtime = Runtime(
        hass, Settings("127.0.0.1", unused_tcp_port_factory(), 0, "127.0.0.1"), "retained-test"
    )
    old = datetime.now(UTC) - timedelta(seconds=1)
    runtime.proxy._update(device_connected=True, valve="open", valve_updated=old)
    publisher = Publisher(hass, runtime, "synthetic/retained")
    publisher.start()
    try:
        await eventually(lambda: bool(payloads(mqtt_mock, publisher.prefix)))
        assert all(not item["available"] for item in payloads(mqtt_mock, publisher.prefix))
        runtime.proxy._update(valve_updated=datetime.now(UTC))
        await eventually(
            lambda: any(item["available"] for item in payloads(mqtt_mock, publisher.prefix))
        )
    finally:
        await publisher.stop()


async def test_publish_failure_does_not_stop_native_control(
    hass, mqtt_mock, unused_tcp_port_factory
):
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.evodnik.mqtt import Publisher
    from custom_components.evodnik.proxy import Settings
    from custom_components.evodnik.runtime import Runtime

    runtime = Runtime(
        hass, Settings("127.0.0.1", unused_tcp_port_factory(), 0, "127.0.0.1"), "failure-test"
    )
    await runtime.start()
    publisher = Publisher(hass, runtime, "synthetic/failure")
    device = await Device.connect(runtime.proxy.bound_port)
    with patch(
        "homeassistant.components.mqtt.async_publish",
        side_effect=HomeAssistantError("Broker unavailable"),
    ):
        publisher.start()
        try:
            await eventually(lambda: runtime.proxy.state.device_connected)
            await runtime.proxy.set_valve("closed")
            assert runtime.valve == "closed"
            assert not publisher._task.done()
        finally:
            await publisher.stop()
            await device.close()
            await runtime.stop()
