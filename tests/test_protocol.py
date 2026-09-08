"""Framing, CRC, matching, and telemetry checks on synthetic data."""

import pytest

from custom_components.evodnik.protocol import (
    MAX_FRAME_SIZE,
    Parser,
    ProtocolError,
    crc16,
    decode,
    encode,
    read_status,
    status_values,
)


def test_standard_crc_check_vector():
    assert crc16(b"123456789") == 0x906E


def test_split_escaped_and_coalesced_frames():
    frame = encode(1, 0x83, 0x72, 0x1234, 3, b"\xc0\xc1\x7d")
    assert b"\x7d\xe0\x7d\xe1\x7d\x5d" in frame.wire
    for split in range(1, len(frame.wire)):
        parser = Parser()
        assert parser.feed(frame.wire[:split]) == []
        assert parser.feed(frame.wire[split:] + frame.wire) == [frame, frame]
        parser.finish()


def test_start_byte_is_not_an_ack():
    parser = Parser()
    assert parser.feed(b"\xc0") == []
    assert parser.pending
    with pytest.raises(ProtocolError):
        parser.finish()


@pytest.mark.parametrize("wire", [b"", b"\xc0\xc1", b"\xc0\xc0", b"\x00", b"\xc0\x7d\xc1"])
def test_invalid_frames(wire):
    with pytest.raises(ProtocolError):
        decode(wire)


def test_crc_and_buffer_limits():
    wire = bytearray(read_status(0x83).wire)
    wire[2] ^= 1
    with pytest.raises(ProtocolError, match="CRC"):
        Parser().feed(wire)
    with pytest.raises(ProtocolError, match="size"):
        Parser().feed(b"\xc0" + bytes(MAX_FRAME_SIZE))


def test_response_matching_checks_tag_opcode_and_known_shape():
    request = read_status(0x83)
    good = encode(0x83, 1, 0x31, 0x20, 16, bytes(16))
    assert good.matches(request)
    assert not encode(0x84, 1, 0x31, 0x20, 16, bytes(16)).matches(request)
    assert not encode(0x83, 1, 0x20, 0x20, 16, bytes(16)).matches(request)
    assert not encode(0x83, 1, 0x31, 0x20, 16, bytes(15)).matches(request)


def test_snapshot_and_unknown_state():
    payload = bytearray(128)
    payload[45] = 0xC8
    payload[48:52] = (42).to_bytes(4, "little")
    assert status_values(encode(0x83, 1, 0x31, 0, 128, payload)) == ("closed", 42)
    assert status_values(encode(0x83, 1, 0x31, 0x20, 16, bytes(16))) == (None, None)
    assert status_values(read_status(0x83)) is None
