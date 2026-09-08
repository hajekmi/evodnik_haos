"""Loopback device and cloud peers with deliberately synthetic telemetry."""

import asyncio
from collections.abc import Callable

from custom_components.evodnik.protocol import Frame, Parser, encode
from custom_components.evodnik.proxy import close_writer


async def eventually(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class Peer:
    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self.writer = writer
        self.parser = Parser()
        self.pending = []

    async def receive(self) -> Frame:
        async with asyncio.timeout(2):
            while not self.pending:
                data = await self.reader.read(4096)
                if not data:
                    raise EOFError("Peer disconnected")
                self.pending.extend(self.parser.feed(data))
        return self.pending.pop(0)

    async def send(self, frame: Frame, *, split: bool = False) -> None:
        if split:
            self.writer.write(frame.wire[:1])
            await self.writer.drain()
            await asyncio.sleep(0)
            self.writer.write(frame.wire[1:])
        else:
            self.writer.write(frame.wire)
        await self.writer.drain()

    async def close(self) -> None:
        await close_writer(self.writer)


class CloudServer:
    def __init__(self) -> None:
        self.connections = asyncio.Queue()
        self.peers = []
        self.server = None

    async def start(self, port=0) -> None:
        def connected(reader, writer):
            peer = Peer(reader, writer)
            self.peers.append(peer)
            self.connections.put_nowait(peer)

        self.server = await asyncio.start_server(connected, "127.0.0.1", port)

    @property
    def port(self) -> int:
        return self.server.sockets[0].getsockname()[1]

    async def connection(self) -> Peer:
        async with asyncio.timeout(2):
            return await self.connections.get()

    async def stop(self) -> None:
        if self.server:
            self.server.close()
        for peer in self.peers:
            await peer.close()
        if self.server:
            await self.server.wait_closed()


class Device(Peer):
    def __init__(self, reader, writer) -> None:
        super().__init__(reader, writer)
        self.valve = "open"
        self.counter = 42
        self.requests: list[Frame] = []
        self.respond = True
        self.apply_commands = True
        self.task = None

    @classmethod
    async def connect(cls, port: int, *, automatic: bool = True):
        self = cls(*await asyncio.open_connection("127.0.0.1", port))
        if automatic:
            self.task = asyncio.create_task(self.run())
        return self

    def reply(self, request: Frame) -> Frame:
        address = request.address
        length = request.length
        if request.valve_command:
            if self.apply_commands:
                self.valve = request.valve_command
            address = length = 0
            payload = b""
        elif request.opcode == 0x31 and address in (0, 0x20):
            payload = bytearray(length)
            payload[45 if address == 0 else 13] = {"open": 0xC7, "closed": 0xC8}[self.valve]
            if address == 0:
                payload[48:52] = self.counter.to_bytes(4, "little")
            payload = bytes(payload)
        else:
            payload = request.payload
        return encode(request.source, request.destination, request.opcode, address, length, payload)

    async def run(self) -> None:
        try:
            while True:
                request = await self.receive()
                self.requests.append(request)
                if self.respond:
                    await self.send(self.reply(request), split=True)
        except EOFError, OSError:
            return

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await super().close()
