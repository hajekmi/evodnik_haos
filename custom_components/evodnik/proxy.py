"""Local TCP proxy with session-scoped transactions and no HA dependencies."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from .protocol import Frame, Parser, ProtocolError, read_snapshot, read_status, status_values
from .protocol import valve_command as make_valve_command

_LOGGER = logging.getLogger(__name__)


class DeviceUnavailable(Exception):
    """The device session is unavailable or has been invalidated."""


class QueueFull(Exception):
    """The bounded request queue cannot accept another operation."""


class CommandNotConfirmed(Exception):
    """The device did not report the requested state after acknowledging a write."""


@dataclass(frozen=True, slots=True)
class Settings:
    target_host: str
    target_port: int
    listen_port: int
    listen_host: str = "0.0.0.0"
    request_timeout: float = 10.0
    write_timeout: float = 5.0
    connect_timeout: float = 5.0
    poll_interval: float = 45.0
    snapshot_interval: float = 300.0
    initial_poll_delay: float = 0.5
    reconnect_delay: float = 1.0
    reconnect_max_delay: float = 60.0
    queue_limit: int = 64


@dataclass(frozen=True, slots=True)
class State:
    device_connected: bool = False
    cloud_connected: bool = False
    valve: str | None = None
    valve_updated: datetime | None = None
    total_liters: int | None = None
    last_response: datetime | None = None
    counter_updated: datetime | None = None
    error: str | None = None


@dataclass(eq=False, slots=True)
class Cloud:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    alive: bool = True


@dataclass(slots=True)
class Request:
    frame: Frame
    cloud: Cloud | None = None
    result: asyncio.Future[Frame] | None = None


@dataclass(eq=False, slots=True)
class Session:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    queue: asyncio.Queue[Request]
    alive: bool = True
    task: asyncio.Task | None = None
    cloud: Cloud | None = None
    current: tuple[Frame, asyncio.Future[Frame]] | None = None
    active_request: Request | None = None
    tag: int = 0x80
    last_status: float | None = None
    last_snapshot: float | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)


async def close_writer(writer: asyncio.StreamWriter) -> None:
    """Close without allowing an unresponsive peer to block integration unload."""
    writer.close()
    try:
        async with asyncio.timeout(1.0):
            await writer.wait_closed()
    except OSError, TimeoutError:
        writer.transport.abort()


async def read_stream(reader: asyncio.StreamReader, frame_timeout: float) -> AsyncIterator[Frame]:
    """Apply an absolute deadline to partial frames, including trickled bytes."""
    parser = Parser()
    deadline = None
    while True:
        async with asyncio.timeout_at(deadline):
            data = await reader.read(4096)
        if not data:
            parser.finish()
            return
        frames = parser.feed(data)
        if not parser.pending:
            deadline = None
        elif deadline is None or frames:
            deadline = asyncio.get_running_loop().time() + frame_timeout
        for frame in frames:
            yield frame


class Proxy:
    """Own the listener and isolate every device/cloud connection generation."""

    def __init__(self, settings: Settings, on_change: Callable[[], None]) -> None:
        self.settings = settings
        self.state = State()
        self.on_change = on_change
        self.dropped_messages = 0
        self._server: asyncio.Server | None = None
        self._session: Session | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    @property
    def bound_port(self) -> int | None:
        if self._server and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return None

    @property
    def queue_size(self) -> int:
        return self._session.queue.qsize() if self._session else 0

    def _update(self, **changes) -> None:
        self.state = replace(self.state, **changes)
        try:
            self.on_change()
        except Exception:
            _LOGGER.error("State notification failed")

    async def start(self) -> None:
        if self._server is not None:
            return
        self._closing = False
        self._server = await asyncio.start_server(
            self._accept, self.settings.listen_host, self.settings.listen_port
        )

    async def stop(self) -> None:
        self._closing = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
            server.close_clients()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)
        if server is not None:
            await server.wait_closed()
        self._session = None
        self._update(
            device_connected=False,
            cloud_connected=False,
            valve=None,
            total_liters=None,
            counter_updated=None,
        )

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing:
            writer.close()
            return
        previous = self._session
        if previous:
            previous.alive = False
            previous.writer.close()
            if previous.cloud:
                previous.cloud.alive = False
                previous.cloud.writer.close()
            if previous.task:
                previous.task.cancel()
        session = Session(reader, writer, asyncio.Queue(self.settings.queue_limit))
        self._session = session
        self._update(
            device_connected=False,
            cloud_connected=False,
            valve=None,
            total_liters=None,
            counter_updated=None,
            last_response=None,
            error=None,
        )
        task = asyncio.create_task(self._run_session(session), name="evodnik session")
        session.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _is_current(self, session: Session) -> bool:
        return not self._closing and session.alive and self._session is session

    def _next_tag(self, session: Session) -> int:
        # Requests are serialized and ambiguous timeouts retire the whole session.
        session.tag = 0x80 + ((session.tag - 0x80 + 1) % 64)
        return session.tag

    async def _submit(self, session: Session, frame: Frame) -> Frame:
        if not self._is_current(session):
            raise DeviceUnavailable("Device unavailable")
        result = asyncio.get_running_loop().create_future()
        try:
            session.queue.put_nowait(Request(frame, result=result))
        except asyncio.QueueFull:
            raise QueueFull("Request queue is full") from None
        return await result

    async def set_valve(self, state: str) -> None:
        if state not in ("open", "closed"):
            raise ValueError("Invalid valve state")
        session = self._session
        if session is None or not self._is_current(session):
            raise DeviceUnavailable("Device unavailable")
        response = await self._submit(session, make_valve_command(self._next_tag(session), state))
        if not self._is_current(session):
            raise DeviceUnavailable("Device unavailable")
        values = status_values(response)
        if values is None or values[0] != state:
            raise CommandNotConfirmed("Requested valve state has not been confirmed")

    async def refresh(self) -> None:
        """Read fresh telemetry without sending the query or response to the cloud."""
        session = self._session
        if session is None or not self._is_current(session):
            raise DeviceUnavailable("Device unavailable")
        await self._submit(session, read_snapshot(self._next_tag(session)))

    async def _run_session(self, session: Session) -> None:
        try:
            try:
                async with asyncio.TaskGroup() as group:
                    session.tasks = [
                        group.create_task(self._device_reader(session)),
                        group.create_task(self._worker(session)),
                        group.create_task(self._cloud_loop(session)),
                        group.create_task(self._poll(session)),
                    ]
            except* OSError, TimeoutError, ProtocolError, DeviceUnavailable:
                if self._is_current(session):
                    self._update(error="session_lost")
            except* Exception:
                _LOGGER.error("Device session failed; reconnect required")
                if self._is_current(session):
                    self._update(error="session_error")
        finally:
            session.alive = False
            if self._session is session:
                self._session = None
                self._update(
                    device_connected=False,
                    cloud_connected=False,
                    valve=None,
                    valve_updated=None,
                    total_liters=None,
                    counter_updated=None,
                )
            if session.current:
                session.current[1].cancel()
                session.current = None
            requests = []
            if session.active_request:
                requests.append(session.active_request)
            while not session.queue.empty():
                requests.append(session.queue.get_nowait())
                session.queue.task_done()
            for request in requests:
                if request.cloud is not None:
                    self.dropped_messages += 1
                if request.result is not None and not request.result.done():
                    request.result.set_exception(DeviceUnavailable("Device unavailable"))
            if session.cloud:
                session.cloud.alive = False
                await close_writer(session.cloud.writer)
            await close_writer(session.writer)

    async def _device_reader(self, session: Session) -> None:
        async for frame in read_stream(session.reader, self.settings.request_timeout):
            if not self._is_current(session):
                return
            pending = session.current
            if pending is None or pending[1].done() or not frame.matches(pending[0]):
                raise ProtocolError("Unmatched device response")
            pending[1].set_result(frame)
        raise DeviceUnavailable("Device disconnected")

    async def _exchange(self, session: Session, frame: Frame) -> Frame:
        if not self._is_current(session):
            raise DeviceUnavailable("Device unavailable")
        result = asyncio.get_running_loop().create_future()
        session.current = (frame, result)
        try:
            async with asyncio.timeout(self.settings.write_timeout):
                session.writer.write(frame.wire)
                await session.writer.drain()
            async with asyncio.timeout(self.settings.request_timeout):
                response = await result
            if not self._is_current(session):
                raise DeviceUnavailable("Device unavailable")
            self._observe(session, response)
            return response
        finally:
            result.cancel()
            session.current = None

    def _observe(self, session: Session, response: Frame) -> None:
        now = datetime.now(UTC)
        changes = {"device_connected": True, "last_response": now, "error": None}
        values = status_values(response)
        if values is not None:
            changes["valve"] = values[0]
            changes["valve_updated"] = now
            session.last_status = time.monotonic()
            if values[1] is not None:
                changes.update(total_liters=values[1], counter_updated=now)
                session.last_snapshot = time.monotonic()
        self._update(**changes)

    async def _worker(self, session: Session) -> None:
        while self._is_current(session):
            request = await session.queue.get()
            session.active_request = request
            try:
                if request.result is not None and request.result.cancelled():
                    continue
                cloud = request.cloud
                if cloud and (not cloud.alive or session.cloud is not cloud):
                    self.dropped_messages += 1
                    continue
                if request.frame.valve_command:
                    self._update(valve=None)
                response = await self._exchange(session, request.frame)
                if cloud and cloud.alive and session.cloud is cloud:
                    try:
                        async with asyncio.timeout(self.settings.write_timeout):
                            cloud.writer.write(response.wire)
                            await cloud.writer.drain()
                    except OSError, TimeoutError:
                        cloud.alive = False
                        cloud.writer.close()
                if request.frame.valve_command:
                    if response.address or response.length or response.payload:
                        raise ProtocolError("Unrecognized valve acknowledgment")
                    response = await self._exchange(session, read_status(self._next_tag(session)))
                if request.result is not None and not request.result.done():
                    request.result.set_result(response)
            finally:
                session.queue.task_done()
                # Preserve a failed operation until session cleanup rejects its waiter.
                if request.result is None or request.result.done():
                    session.active_request = None

    def _enqueue_cloud(self, session: Session, cloud: Cloud, frame: Frame) -> None:
        if not self._is_current(session) or not cloud.alive or session.cloud is not cloud:
            self.dropped_messages += 1
            return
        try:
            session.queue.put_nowait(Request(frame, cloud=cloud))
        except asyncio.QueueFull:
            self.dropped_messages += 1
            raise QueueFull("Cloud request queue is full") from None

    async def _cloud_loop(self, session: Session) -> None:
        delay = self.settings.reconnect_delay
        while self._is_current(session):
            cloud = None
            try:
                async with asyncio.timeout(self.settings.connect_timeout):
                    reader, writer = await asyncio.open_connection(
                        self.settings.target_host, self.settings.target_port
                    )
                cloud = Cloud(reader, writer)
                if not self._is_current(session):
                    return
                session.cloud = cloud
                self._update(cloud_connected=True)
                async for frame in read_stream(reader, self.settings.request_timeout):
                    self._enqueue_cloud(session, cloud, frame)
                    delay = self.settings.reconnect_delay
            except OSError, TimeoutError, ProtocolError, QueueFull:
                pass
            finally:
                if cloud:
                    cloud.alive = False
                    await close_writer(cloud.writer)
                if session.cloud is cloud:
                    session.cloud = None
                if self._is_current(session):
                    self._update(cloud_connected=False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.settings.reconnect_max_delay)

    async def _poll(self, session: Session) -> None:
        await asyncio.sleep(self.settings.initial_poll_delay)
        while self._is_current(session):
            now = time.monotonic()
            frame = None
            if (
                session.last_snapshot is None
                or now - session.last_snapshot >= self.settings.snapshot_interval
            ):
                frame = read_snapshot(self._next_tag(session))
            elif (
                session.last_status is None
                or now - session.last_status >= self.settings.poll_interval
            ):
                frame = read_status(self._next_tag(session))
            if frame:
                try:
                    await self._submit(session, frame)
                except QueueFull:
                    pass
            await asyncio.sleep(self.settings.poll_interval)
