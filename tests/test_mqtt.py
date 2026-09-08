"""Exercise real HA discovery and MQTT commands against a loopback water device."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from homeassistant.components import mqtt
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from .simulators import CloudServer, Device, eventually


@pytest.fixture(autouse=True)
def close_mock_mqtt_socket(mqtt_mock, mqtt_client_mock):
    """Deliver live publications as a broker would and close the mock socket."""
    publish = mqtt_client_mock.publish.side_effect

    def live_publish(topic, payload, qos, retain, properties=None):
        # RETAIN is cleared on live delivery; only a stored replay carries it.
        return publish(topic, payload, qos, False, properties)

    mqtt_client_mock.publish.side_effect = live_publish
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


def mqtt_entity(hass, entry, component, key):
    return er.async_get(hass).async_get_entity_id(
        component, "mqtt", f"evodnik_{entry.entry_id}_mqtt_{key}"
    )


@pytest.fixture
async def mqtt_system(hass, mqtt_mock, unused_tcp_port_factory):
    entry = MockConfigEntry(
        domain="evodnik",
        title="eVodnik",
        data={
            "target_host": "127.0.0.1",
            "target_port": unused_tcp_port_factory(),
            "listen_host": "127.0.0.1",
            "listen_port": unused_tcp_port_factory(),
            "mqtt_enabled": True,
            "mqtt_prefix": "synthetic/control",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    device = await Device.connect(entry.runtime_data.proxy.bound_port)
    try:
        await eventually(lambda: entry.runtime_data.publisher._payload()["available"])
        await eventually(
            lambda: all(
                mqtt_entity(hass, entry, component, key)
                for component, key in (
                    ("valve", "water"),
                    ("sensor", "water_total"),
                    ("sensor", "water_meter"),
                    ("binary_sensor", "device_connected"),
                    ("binary_sensor", "cloud_connected"),
                )
            )
        )
        await hass.async_block_till_done()
        # The mock broker delivers live messages but does not replay stored state
        # for subscriptions created by discovery. Send the next regular report.
        entry.runtime_data.notify()
        await eventually(
            lambda: hass.states.get(mqtt_entity(hass, entry, "valve", "water")).state == "open"
        )
        yield entry, device
    finally:
        await device.close()
        if hasattr(entry, "runtime_data") and entry.runtime_data.proxy.bound_port is not None:
            await hass.config_entries.async_unload(entry.entry_id)


async def test_discovery_creates_mqtt_device_with_controls_and_counters(hass, mqtt_system):
    entry, device = mqtt_system
    registry = er.async_get(hass)
    valve_id = mqtt_entity(hass, entry, "valve", "water")
    native = registry.async_get("valve.evodnik_water")
    mirrored = registry.async_get(valve_id)
    assert native.device_id != mirrored.device_id
    device_entry = dr.async_get(hass).async_get(mirrored.device_id)
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    assert mqtt_entry.entry_id in device_entry.config_entries
    assert device_entry.name == "eVodnik MQTT"
    assert (
        registry.async_get(mqtt_entity(hass, entry, "sensor", "water_total")).device_id
        == mirrored.device_id
    )
    assert hass.states.get(mqtt_entity(hass, entry, "sensor", "water_total")).state == "42"
    assert hass.states.get(mqtt_entity(hass, entry, "sensor", "water_meter")).state == "unknown"
    await entry.runtime_data.set_meter_reading(12000)
    device.counter += 7
    await entry.runtime_data.proxy.refresh()
    meter_id = mqtt_entity(hass, entry, "sensor", "water_meter")
    await eventually(lambda: hass.states.get(meter_id).state == "12007")
    assert hass.states.get(meter_id).attributes["calibration_status"] == "ready"
    assert (
        hass.states.get(mqtt_entity(hass, entry, "binary_sensor", "device_connected")).state == "on"
    )
    assert (
        hass.states.get(mqtt_entity(hass, entry, "binary_sensor", "cloud_connected")).state == "off"
    )
    for service, state in (("close_valve", "closed"), ("open_valve", "open")):
        await hass.services.async_call("valve", service, {"entity_id": valve_id}, blocking=True)
        await eventually(
            lambda state=state: device.valve == state and hass.states.get(valve_id).state == state
        )
        assert hass.states.get("valve.evodnik_water").state == state
    assert [request.valve_command for request in device.requests if request.valve_command] == [
        "closed",
        "open",
    ]


@pytest.mark.parametrize(
    "payload,retained",
    [
        ("CLOSE", True),
        ("OPEN", True),
        ("STOP", False),
        ("close", False),
        ('{"state":"closed"}', False),
    ],
)
async def test_retained_and_invalid_commands_never_reach_device(
    hass, mqtt_system, payload, retained
):
    entry, device = mqtt_system
    async_fire_mqtt_message(hass, "synthetic/control/valve/set", payload, retain=retained)
    await hass.async_block_till_done()
    assert not any(request.valve_command for request in device.requests)
    assert entry.runtime_data.valve == "open"


async def test_command_acknowledgment_does_not_optimistically_change_state(
    hass, mqtt_system, caplog
):
    entry, device = mqtt_system
    device.apply_commands = False
    valve_id = mqtt_entity(hass, entry, "valve", "water")
    async_fire_mqtt_message(hass, "synthetic/control/valve/set", "CLOSE")
    await eventually(lambda: "MQTT valve command was not confirmed" in caplog.text)
    await eventually(lambda: hass.states.get(valve_id).state == "open")
    assert entry.runtime_data.valve == "open"
    assert [request.valve_command for request in device.requests if request.valve_command] == [
        "closed"
    ]


async def test_command_burst_and_device_replacement_do_not_replay(hass, mqtt_system):
    entry, device = mqtt_system
    device.respond = False
    async_fire_mqtt_message(hass, "synthetic/control/valve/set", "CLOSE")
    await eventually(lambda: any(request.valve_command for request in device.requests))
    for _ in range(100):
        async_fire_mqtt_message(hass, "synthetic/control/valve/set", "OPEN")
    await hass.async_block_till_done()
    assert entry.runtime_data.proxy._session.queue.qsize() <= 1
    replacement = await Device.connect(entry.runtime_data.proxy.bound_port)
    try:
        await eventually(lambda: entry.runtime_data.valve == "open")
        await hass.async_block_till_done()
        assert not any(request.valve_command for request in replacement.requests)
    finally:
        await replacement.close()


async def test_offline_commands_are_discarded_and_connectivity_remains_visible(hass, mqtt_system):
    entry, device = mqtt_system
    await device.close()
    await eventually(lambda: not entry.runtime_data.proxy.state.device_connected)
    connected_id = mqtt_entity(hass, entry, "binary_sensor", "device_connected")
    valve_id = mqtt_entity(hass, entry, "valve", "water")
    await eventually(lambda: hass.states.get(connected_id).state == "off")
    assert hass.states.get(valve_id).state == "unavailable"
    async_fire_mqtt_message(hass, "synthetic/control/valve/set", "CLOSE")
    await hass.async_block_till_done()
    replacement = await Device.connect(entry.runtime_data.proxy.bound_port)
    try:
        await eventually(lambda: hass.states.get(valve_id).state == "open")
        assert not any(request.valve_command for request in replacement.requests)
    finally:
        await replacement.close()


async def test_vendor_command_updates_mqtt_without_command_feedback(hass, mqtt_system, mqtt_mock):
    from custom_components.evodnik.protocol import valve_command

    entry, device = mqtt_system
    cloud = CloudServer()
    await cloud.start(entry.data["target_port"])
    try:
        peer = await cloud.connection()
        command = valve_command(0x88, "closed")
        await peer.send(command)
        await peer.receive()
        valve_id = mqtt_entity(hass, entry, "valve", "water")
        await eventually(lambda: hass.states.get(valve_id).state == "closed")
        assert [request.valve_command for request in device.requests if request.valve_command] == [
            "closed"
        ]
        assert not any(
            call.args[0].endswith("/valve/set") for call in mqtt_mock.async_publish.call_args_list
        )
    finally:
        await cloud.stop()


async def test_reconfiguration_preserves_ids_and_removes_old_topics(hass, mqtt_system, mqtt_mock):
    entry, _device = mqtt_system
    valve_id = mqtt_entity(hass, entry, "valve", "water")
    old_prefix = entry.data["mqtt_prefix"]
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "mqtt_prefix": "synthetic/changed"}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    replacement = await Device.connect(entry.runtime_data.proxy.bound_port)
    try:
        await eventually(lambda: hass.states.get(valve_id).state == "open")
        assert mqtt_entity(hass, entry, "valve", "water") == valve_id
        for suffix in ("state", "availability", "bridge_availability"):
            assert any(
                call.args[0] == f"{old_prefix}/{suffix}" and call.args[1] in ("", b"")
                for call in mqtt_mock.async_publish.call_args_list
            )
        async_fire_mqtt_message(hass, f"{old_prefix}/valve/set", "CLOSE")
        await hass.async_block_till_done()
        assert not any(request.valve_command for request in replacement.requests)
        await hass.services.async_call(
            "valve", "close_valve", {"entity_id": valve_id}, blocking=True
        )
        await eventually(lambda: replacement.valve == "closed")
    finally:
        await replacement.close()


@pytest.mark.parametrize("remove", [False, True])
async def test_disabled_or_deleted_mqtt_is_removed_after_broker_recovers(
    hass, mqtt_system, mqtt_mock, remove
):
    entry, _device = mqtt_system
    valve_id = mqtt_entity(hass, entry, "valve", "water")
    mqtt_mock.connected = False
    async_dispatcher_send(hass, mqtt.MQTT_CONNECTION_STATE, False)
    if remove:
        await hass.config_entries.async_remove(entry.entry_id)
    else:
        hass.config_entries.async_update_entry(entry, data={**entry.data, "mqtt_enabled": False})
        assert await hass.config_entries.async_reload(entry.entry_id)
    mqtt_mock.connected = True
    async_dispatcher_send(hass, mqtt.MQTT_CONNECTION_STATE, True)
    await eventually(lambda: er.async_get(hass).async_get(valve_id) is None)
    assert any(
        call.args[0].endswith("/water/config") and call.args[1] in ("", b"")
        for call in mqtt_mock.async_publish.call_args_list
    )


@pytest.mark.parametrize(
    "mqtt_config_entry_options",
    [
        {
            "discovery_prefix": "synthetic/discovery",
            "birth_message": {
                "topic": "synthetic/ha_status",
                "payload": "ready",
                "qos": 0,
                "retain": False,
            },
            "will_message": {
                "topic": "synthetic/ha_status",
                "payload": "gone",
                "qos": 0,
                "retain": False,
            },
        }
    ],
)
async def test_custom_discovery_birth_and_will(hass, mqtt_system, mqtt_mock):
    entry, device = mqtt_system
    valve_id = mqtt_entity(hass, entry, "valve", "water")
    discovery_topic = f"synthetic/discovery/valve/evodnik_{entry.entry_id}/water/config"
    count = sum(call.args[0] == discovery_topic for call in mqtt_mock.async_publish.call_args_list)
    assert count
    await eventually(lambda: entry.runtime_data.proxy._session.current is None)
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, "synthetic/ha_status", "gone")
    await eventually(lambda: hass.states.get(valve_id).state == "unavailable")
    device.respond = False
    async_fire_mqtt_message(hass, "synthetic/ha_status", "ready")
    await eventually(
        lambda: (
            sum(call.args[0] == discovery_topic for call in mqtt_mock.async_publish.call_args_list)
            > count
        )
    )
    await eventually(lambda: hass.states.get(valve_id).state == "unavailable")
    assert not any(request.valve_command for request in device.requests)


async def test_broker_disconnect_cancels_an_unsent_command(hass, mqtt_system, mqtt_mock):
    entry, device = mqtt_system
    runtime = entry.runtime_data
    await eventually(lambda: runtime.proxy._session.current is None)
    device.respond = False
    before = len(device.requests)
    refresh = asyncio.create_task(runtime.proxy.refresh())
    try:
        await eventually(lambda: len(device.requests) > before)
        blocking_request = device.requests[-1]
        async_fire_mqtt_message(hass, "synthetic/control/valve/set", "CLOSE")
        await eventually(lambda: runtime.publisher._command_task is not None)
        mqtt_mock.connected = False
        async_dispatcher_send(hass, mqtt.MQTT_CONNECTION_STATE, False)
        await hass.async_block_till_done()
        device.respond = True
        await device.send(device.reply(blocking_request))
        await refresh
        mqtt_mock.connected = True
        async_dispatcher_send(hass, mqtt.MQTT_CONNECTION_STATE, True)
        await eventually(lambda: runtime.publisher._payload()["available"])
        assert not any(request.valve_command for request in device.requests)
    finally:
        refresh.cancel()
        await asyncio.gather(refresh, return_exceptions=True)


async def test_pending_discovery_cleanup_survives_registry_restart(hass, mqtt_system, mqtt_mock):
    from custom_components.evodnik.mqtt_discovery import DATA_REGISTRY, async_get_registry

    entry, _device = mqtt_system
    valve_id = mqtt_entity(hass, entry, "valve", "water")
    mqtt_mock.connected = False
    async_dispatcher_send(hass, mqtt.MQTT_CONNECTION_STATE, False)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "mqtt_enabled": False})
    assert await hass.config_entries.async_reload(entry.entry_id)
    previous = await async_get_registry(hass)
    assert previous.pending
    await previous.stop()
    hass.data.pop(DATA_REGISTRY)
    restored = await async_get_registry(hass)
    assert restored.pending == previous.pending
    mqtt_mock.connected = True
    async_dispatcher_send(hass, mqtt.MQTT_CONNECTION_STATE, True)
    await eventually(lambda: er.async_get(hass).async_get(valve_id) is None)
    await eventually(lambda: not restored.pending)
