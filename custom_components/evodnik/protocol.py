"""Bounded framing and known message shapes for the eVodnik protocol."""

from dataclasses import dataclass

START = 0xC0
END = 0xC1
ESCAPE = 0x7D
MAX_FRAME_SIZE = 4096
DEVICE_ADDRESS = 0x01
OPEN_CODE = 0xC7
CLOSE_CODE = 0xC8


class ProtocolError(Exception):
    """Invalid framing or an ambiguous response; messages contain no wire data."""


def crc16(data: bytes) -> int:
    """Return CRC-16/X-25; this protocol transmits its result big endian."""
    value = 0xFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x8408 if value & 1 else 0)
    return value ^ 0xFFFF


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded frame retaining the exact original bytes for forwarding."""

    destination: int
    source: int
    opcode: int
    address: int
    length: int
    payload: bytes
    wire: bytes

    @property
    def valve_command(self) -> str | None:
        if self.opcode == 0x20 and self.address == 0x1A and self.length == 2:
            return {b"\x00\xc7": "open", b"\x00\xc8": "closed"}.get(self.payload)
        return None

    def matches(self, request: Frame) -> bool:
        """Match observed endpoint/opcode semantics without guessing unknown data."""
        if (self.destination, self.source, self.opcode) != (
            request.source,
            request.destination,
            request.opcode,
        ):
            return False
        if request.opcode in (0x31, 0x41) and not request.payload:
            return (
                self.address == request.address
                and self.length == request.length
                and len(self.payload) == self.length
            )
        return True


def decode(wire: bytes) -> Frame:
    """Validate a complete frame; never include private bytes in exceptions."""
    if len(wire) > MAX_FRAME_SIZE or not wire or wire[0] != START or wire[-1] != END:
        raise ProtocolError("Invalid frame delimiters or size")
    body = bytearray()
    escaped = False
    for byte in wire[1:-1]:
        if escaped:
            body.append(byte ^ 0x20)
            escaped = False
        elif byte == ESCAPE:
            escaped = True
        elif byte in (START, END):
            raise ProtocolError("Unexpected frame delimiter")
        else:
            body.append(byte)
    if escaped or len(body) < 10:
        raise ProtocolError("Incomplete frame")
    if crc16(body[:-2]) != int.from_bytes(body[-2:], "big"):
        raise ProtocolError("Invalid frame CRC")
    return Frame(
        body[0],
        body[1],
        body[2],
        int.from_bytes(body[3:7], "big"),
        body[7],
        bytes(body[8:-2]),
        wire,
    )


def encode(
    destination: int, source: int, opcode: int, address: int, length: int, payload: bytes = b""
) -> Frame:
    """Create a frame; escaping END and ESCAPE still needs firmware verification."""
    body = bytes((destination, source, opcode)) + address.to_bytes(4, "big")
    body += bytes((length,)) + payload
    body += crc16(body).to_bytes(2, "big")
    wire = bytearray((START,))
    for byte in body:
        if byte in (START, END, ESCAPE):
            wire.extend((ESCAPE, byte ^ 0x20))
        else:
            wire.append(byte)
    wire.append(END)
    return decode(bytes(wire))


def read_status(tag: int) -> Frame:
    return encode(DEVICE_ADDRESS, tag, 0x31, 0x20, 16)


def read_snapshot(tag: int) -> Frame:
    return encode(DEVICE_ADDRESS, tag, 0x31, 0, 128)


def valve_command(tag: int, state: str) -> Frame:
    code = {"open": OPEN_CODE, "closed": CLOSE_CODE}[state]
    return encode(DEVICE_ADDRESS, tag, 0x20, 0x1A, 2, bytes((0, code)))


def status_values(frame: Frame) -> tuple[str | None, int | None] | None:
    """Extract reported state and the raw lower 32 counter bits, when present."""
    if frame.opcode != 0x31 or len(frame.payload) != frame.length:
        return None
    counter = None
    if frame.address == 0x20 and frame.length == 16:
        status = frame.payload
    elif frame.address == 0 and frame.length == 128:
        status = frame.payload[32:48]
        counter = int.from_bytes(frame.payload[48:52], "little")
    else:
        return None
    return {OPEN_CODE: "open", CLOSE_CODE: "closed"}.get(status[13]), counter


class Parser:
    """Incremental parser supporting split escapes and coalesced TCP frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._escaped = False

    @property
    def pending(self) -> bool:
        return bool(self._buffer)

    def feed(self, data: bytes) -> list[Frame]:
        frames = []
        for byte in data:
            if not self._buffer:
                if byte != START:
                    raise ProtocolError("Missing frame start")
                self._buffer.append(byte)
                continue
            self._buffer.append(byte)
            if len(self._buffer) > MAX_FRAME_SIZE:
                raise ProtocolError("Frame size limit exceeded")
            if self._escaped:
                self._escaped = False
            elif byte == ESCAPE:
                self._escaped = True
            elif byte == START:
                raise ProtocolError("Unexpected frame start")
            elif byte == END:
                frames.append(decode(bytes(self._buffer)))
                self._buffer.clear()
        return frames

    def finish(self) -> None:
        if self.pending:
            raise ProtocolError("Stream ended inside a frame")
