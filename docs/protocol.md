# Protocol notes

This document describes generalized findings from a private capture. It contains
no captured transactions, deployment endpoints, device identities, or operational
timeline. All public tests generate synthetic messages.

## Wire envelope

```text
c0 | escaped(header + data + crc) | c1

header:
  destination     1 byte
  source/tag      1 byte
  opcode          1 byte
  address         4 bytes, big endian
  declared length 1 byte
crc:
  CRC-16/X-25     2 bytes, big endian
```

CRC covers the unescaped header and data, excluding the delimiters and CRC
itself. Parameters: polynomial `0x1021` (reflected `0x8408`), initial value
`0xffff`, reflected input/output, final XOR `0xffff`. The standard ASCII check
vector `123456789` yields `0x906e`.

Escape byte `0x7d` means XOR the following byte with `0x20`. Escaping `c0` as
`7d e0` was observed. Encoding `c1` as `7d e1` and `7d` as `7d 5d` follows that
rule but was not observed and requires firmware verification.

A read request has a declared response length and no data. Thus declared length
does not always equal data length. Unknown opcode payloads are kept opaque.
The incremental parser accepts split frames, coalesced frames, and split escape
pairs. A lone `c0` is an incomplete frame, never an acknowledgment.

The observed device protocol address is `0x01`; this is an application protocol
byte, not an IP address or device identity. Server source/tag values were in
`0x80`–`0xbf`. Responses swap the endpoint bytes and keep the opcode. Their
exact tag semantics have not been independently verified.

## Known operations

| Operation | Opcode | Address | Length | Request data |
|---|---|---|---|---|
| Read status | `31` | `00000020` | 16 | Empty |
| Read snapshot | `31` | `00000000` | 128 | Empty |
| Close valve | `20` | `0000001a` | 2 | `00 c8` |
| Open valve | `20` | `0000001a` | 2 | `00 c7` |
| Write clock | `30` | `00000050` | 4 | Opaque in this integration |
| Read history | `41` | Vendor supplied | Record multiples | Empty |

The observed successful valve write acknowledgment has opcode `20`, address
zero, length zero, and no data. This acknowledgment does not confirm the final
reported valve state; a separate status read follows every recognized valve
command, including forwarded vendor commands.

In the 16-byte status response, data offset 13 is `c7` for reported open and
`c8` for reported closed. Other values mean unknown. In the 128-byte snapshot,
status occupies data offsets 32–47, and offsets 48–51 contain the lower 32 bits
of a little-endian water counter. This version assumes one pulse per liter.
It does not infer upper counter bits, rollover, resets, or instantaneous flow.

History uses 32-byte records. History read side effects, buffer capacity, clock
representation, and firmware time zone are outside local control in this
version. These transactions continue to pass through to the original server.

## Transaction ownership

One bounded queue handles vendor and local requests, with one transaction in
flight. Known reads require matching endpoints, opcode, address, declared length,
and response data length. Unknown transactions use the swapped endpoints and
opcode without interpreting the payload. Their original bytes are preserved in
both directions. Unsolicited, duplicate, or unmatched device frames are ambiguous
and close the device session; this version expects request/response traffic.

Local tags cycle within the observed range. Tags are not rewritten in forwarded
frames. Serialization prevents simultaneous tag collisions. A response timeout
retires the whole device session, discards its queue, and rejects pending local
calls. It never retries a write. The next device connection uses a new cloud
connection; old commands and replies cannot cross session generations.

If only the vendor connection fails, the local connection remains. Any already
sent device operation finishes or times out. Its response is discarded if the
originating vendor connection has gone away. Queued requests belonging to that
connection are dropped. Local polling and controls continue during cloud retries.

Limits: 4 KiB encoded frame, 64 queued requests, 10-second response and partial
frame deadlines, 5-second connect/write deadlines, and 1-second graceful writer
close before abort. Partial frame deadlines are absolute, including trickled
bytes. Queue overflow closes the affected vendor session; a full queue rejects
a new local action immediately.

## Verification boundary

Synthetic tests validate the implementation against these message shapes. They
do not establish that live firmware accepts generated source/tag values, all
escape variants, or polling while the vendor is absent. The device's session
idle timeout, startup requirements, command timing, and mechanical valve
feedback remain subject to a controlled test. No network experiment against a
physical device or public vendor endpoint is part of automated validation.
