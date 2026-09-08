"""Exercise actual TCP streams, failures, and session isolation on loopback."""

import asyncio

import pytest

from custom_components.evodnik.protocol import encode, read_snapshot, read_status, valve_command
from custom_components.evodnik.proxy import (
    CommandNotConfirmed,
    DeviceUnavailable,
    Proxy,
    QueueFull,
    Settings,
)

from .simulators import CloudServer, Device, eventually


@pytest.fixture
async def make_proxy():
    resources = []

    async def create(**changes):
        cloud = CloudServer()
        await cloud.start()
        settings = dict(
            target_host="127.0.0.1",
            target_port=cloud.port,
            listen_host="127.0.0.1",
            listen_port=0,
            request_timeout=0.4,
            connect_timeout=0.1,
            initial_poll_delay=0.01,
            poll_interval=0.05,
            snapshot_interval=0.15,
            reconnect_delay=0.02,
            reconnect_max_delay=0.05,
            queue_limit=8,
        )
        settings.update(changes)
        proxy = Proxy(Settings(**settings), lambda: None)
        await proxy.start()
        resources.append((proxy, cloud))
        return proxy, cloud

    yield create
    for proxy, cloud in reversed(resources):
        await proxy.stop()
        await cloud.stop()
        assert not proxy._tasks


async def test_local_commands_and_counter_with_cloud_down(make_proxy):
    proxy, cloud = await make_proxy()
    await cloud.stop()
    device = await Device.connect(proxy.bound_port)
    try:
        await eventually(lambda: proxy.state.total_liters == 42)
        assert not proxy.state.cloud_connected
        await proxy.set_valve("closed")
        assert proxy.state.valve == "closed"
        await proxy.set_valve("open")
        assert proxy.state.valve == "open"
        device.counter = 43
        await eventually(lambda: proxy.state.total_liters == 43)
    finally:
        await device.close()


async def test_exact_unknown_forwarding_and_local_reply_isolation(make_proxy):
    proxy, cloud = await make_proxy()
    device = await Device.connect(proxy.bound_port)
    try:
        vendor = await cloud.connection()
        await proxy.set_valve("closed")
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.03):
                await vendor.reader.read(1)
        frames = [encode(1, 0x81, 0x72, 0x1234, 3, b"\xc0\xc1\x7d"), read_snapshot(0x81)]
        vendor.writer.write(b"".join(frame.wire for frame in frames))
        await vendor.writer.drain()
        responses = [await vendor.receive(), await vendor.receive()]
        assert responses[0].wire == device.reply(frames[0]).wire
        assert responses[1].matches(frames[1])
        assert any(frame.wire == frames[0].wire for frame in device.requests)
        await vendor.send(valve_command(0x81, "open"))
        assert (await vendor.receive()).opcode == 0x20
        await eventually(lambda: proxy.state.valve == "open")
    finally:
        await device.close()


async def test_ack_does_not_confirm_unapplied_command(make_proxy):
    proxy, cloud = await make_proxy()
    device = await Device.connect(proxy.bound_port)
    device.apply_commands = False
    try:
        await eventually(lambda: proxy.state.valve == "open")
        with pytest.raises(CommandNotConfirmed):
            await proxy.set_valve("closed")
        assert proxy.state.valve == "open"
    finally:
        await device.close()


async def test_concurrent_commands_share_tags_but_not_results(make_proxy):
    proxy, cloud = await make_proxy()
    device = await Device.connect(proxy.bound_port)
    try:
        vendor = await cloud.connection()
        commands = [
            asyncio.create_task(proxy.set_valve(state)) for state in ("closed", "open", "closed")
        ]
        await vendor.send(read_status(0x82))
        response = await vendor.receive()
        assert response.matches(read_status(0x82))
        await asyncio.gather(*commands)
        assert [frame.valve_command for frame in device.requests if frame.valve_command] == [
            "closed",
            "open",
            "closed",
        ]
    finally:
        await device.close()


async def test_timeout_start_byte_and_late_response_retire_session(make_proxy):
    proxy, cloud = await make_proxy(initial_poll_delay=60)
    device = await Device.connect(proxy.bound_port, automatic=False)
    try:
        await cloud.connection()
        command = asyncio.create_task(proxy.set_valve("closed"))
        request = await device.receive()
        device.writer.write(b"\xc0")
        await device.writer.drain()
        assert proxy.state.valve is None
        with pytest.raises(DeviceUnavailable):
            await command
        await eventually(lambda: proxy._session is None)
        assert not proxy.state.device_connected
        assert await device.reader.read() == b""
        new_device = await Device.connect(proxy.bound_port)
        try:
            await eventually(lambda: proxy._session is not None)
            await proxy.refresh()
            assert proxy.state.valve == "open"
            # The old transport is closed and cannot satisfy a request in the new session.
            try:
                await device.send(device.reply(request))
            except OSError:
                pass
            assert proxy.state.valve == "open"
            assert not any(frame.valve_command for frame in new_device.requests)
        finally:
            await new_device.close()
    finally:
        await device.close()


@pytest.mark.parametrize("tail", [b"\xc0", b"burst", b"none"])
async def test_disconnect_discards_partial_and_queued_cloud_commands(make_proxy, tail):
    proxy, cloud = await make_proxy(initial_poll_delay=60)
    device = await Device.connect(proxy.bound_port, automatic=False)
    vendor = await cloud.connection()
    session, cloud_session = proxy._session, proxy._session.cloud
    command = valve_command(0x85, "closed")
    await vendor.send(read_status(0x84))
    await device.receive()
    if tail == b"burst":
        vendor.writer.write(command.wire * 5)
    elif tail != b"none":
        vendor.writer.write(tail)
    await vendor.writer.drain()
    await device.close()
    await eventually(lambda: proxy._session is None)
    proxy._enqueue_cloud(session, cloud_session, command)
    assert proxy.dropped_messages >= 1
    with pytest.raises(DeviceUnavailable):
        await proxy.set_valve("open")
    fresh = await Device.connect(proxy.bound_port)
    try:
        await cloud.connection()
        await proxy.refresh()
        assert proxy.state.valve == "open"
        assert all(frame.valve_command is None for frame in fresh.requests)
    finally:
        await fresh.close()


async def test_cloud_reconnect_does_not_receive_old_response(make_proxy):
    proxy, cloud = await make_proxy(initial_poll_delay=60)
    device = await Device.connect(proxy.bound_port, automatic=False)
    try:
        old_vendor = await cloud.connection()
        request = read_status(0x85)
        await old_vendor.send(request)
        assert (await device.receive()).wire == request.wire
        await old_vendor.close()
        new_vendor = await cloud.connection()
        await device.send(device.reply(request))
        await eventually(lambda: proxy.state.device_connected)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.03):
                await new_vendor.reader.read(1)
        await new_vendor.send(read_status(0x86))
        new_request = await device.receive()
        await device.send(device.reply(new_request))
        assert (await new_vendor.receive()).matches(new_request)
    finally:
        await device.close()


async def test_second_device_replaces_first_and_rejects_pending(make_proxy):
    proxy, cloud = await make_proxy(initial_poll_delay=60)
    first = await Device.connect(proxy.bound_port, automatic=False)
    await cloud.connection()
    command = asyncio.create_task(proxy.set_valve("closed"))
    await first.receive()
    second = await Device.connect(proxy.bound_port)
    try:
        with pytest.raises(DeviceUnavailable):
            await command
        assert await first.reader.read() == b""
        await proxy.refresh()
        assert proxy.state.valve == "open"
    finally:
        await first.close()
        await second.close()


async def test_bad_device_crc_never_updates_state(make_proxy):
    proxy, cloud = await make_proxy(initial_poll_delay=60)
    device = await Device.connect(proxy.bound_port, automatic=False)
    try:
        await cloud.connection()
        pending = asyncio.create_task(proxy.refresh())
        request = await device.receive()
        wire = bytearray(device.reply(request).wire)
        wire[2] ^= 1
        device.writer.write(wire)
        await device.writer.drain()
        with pytest.raises(DeviceUnavailable):
            await pending
        assert proxy.state.last_response is None
        assert proxy.state.valve is None
    finally:
        await device.close()


async def test_queue_overflow_closes_only_cloud_and_no_write_replay(make_proxy):
    proxy, cloud = await make_proxy(initial_poll_delay=60, queue_limit=2)
    device = await Device.connect(proxy.bound_port)
    try:
        vendor = await cloud.connection()
        vendor.writer.write(valve_command(0x86, "closed").wire * 10)
        await vendor.writer.drain()
        await cloud.connection()
        await proxy.refresh()
        assert proxy.state.device_connected
        assert proxy.dropped_messages > 0
        assert device.valve == "open"
    finally:
        await device.close()


async def test_stop_releases_listener_and_waiters(make_proxy):
    proxy, cloud = await make_proxy(initial_poll_delay=60)
    device = await Device.connect(proxy.bound_port, automatic=False)
    port = proxy.bound_port
    await cloud.connection()
    command = asyncio.create_task(proxy.set_valve("closed"))
    await device.receive()
    await proxy.stop()
    with pytest.raises(DeviceUnavailable):
        await command
    server = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", port)
    server.close()
    await server.wait_closed()
    await device.close()


async def test_cloud_partial_frame_deadline_is_not_extended_by_bytes(make_proxy):
    proxy, cloud = await make_proxy(request_timeout=0.12)
    device = await Device.connect(proxy.bound_port)
    try:
        vendor = await cloud.connection()
        vendor.writer.write(b"\xc0")
        await vendor.writer.drain()
        for _ in range(3):
            await asyncio.sleep(0.03)
            vendor.writer.write(b"\x00")
            await vendor.writer.drain()
        async with asyncio.timeout(0.08):
            assert await vendor.reader.read() == b""
        await cloud.connection()
        await proxy.set_valve("closed")
        assert proxy.state.valve == "closed"
    finally:
        await device.close()


async def test_full_local_queue_and_cancelled_command_are_not_replayed(make_proxy):
    proxy, cloud = await make_proxy(initial_poll_delay=60, queue_limit=1)
    device = await Device.connect(proxy.bound_port, automatic=False)
    await cloud.connection()
    active = asyncio.create_task(proxy.refresh())
    queued = None
    try:
        request = await device.receive()
        queued = asyncio.create_task(proxy.set_valve("closed"))
        await eventually(lambda: proxy.queue_size == 1)
        with pytest.raises(QueueFull):
            await proxy.set_valve("open")
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        await device.send(device.reply(request))
        await active
        await eventually(lambda: proxy.queue_size == 0)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.03):
                await device.reader.read(1)
    finally:
        active.cancel()
        if queued:
            queued.cancel()
        await asyncio.gather(active, *([queued] if queued else []), return_exceptions=True)
        await device.close()


async def test_disconnect_between_cloud_enqueue_and_device_write(make_proxy, monkeypatch):
    proxy, cloud = await make_proxy(initial_poll_delay=60)
    device = await Device.connect(proxy.bound_port, automatic=False)
    vendor = await cloud.connection()
    reached_write = asyncio.Event()
    original = proxy._exchange

    async def paused_exchange(session, frame):
        reached_write.set()
        await asyncio.Event().wait()
        return await original(session, frame)

    monkeypatch.setattr(proxy, "_exchange", paused_exchange)
    await vendor.send(valve_command(0x86, "closed"))
    await reached_write.wait()
    await device.close()
    await eventually(lambda: proxy._session is None)
    monkeypatch.setattr(proxy, "_exchange", original)
    fresh = await Device.connect(proxy.bound_port)
    try:
        await cloud.connection()
        await proxy.refresh()
        assert proxy.state.valve == "open"
        assert all(frame.valve_command is None for frame in fresh.requests)
    finally:
        await fresh.close()
