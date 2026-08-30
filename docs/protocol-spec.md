# Noventis Wire Protocol Specification

**Status:** This document is the **single source of truth** for the on-wire
format. It documents the format that the production firmware
(`edge/node_tx.py`) and the original base-station receiver
(`backend/legacy/base_rx.py`) already speak. Both `edge/protocol.py` and
`backend/ingest/protocol.py` implement it and **must stay byte-for-byte
identical** to each other. Any change to framing or tags is made here **and** in
both modules, in the same commit.

There is **no protocol version byte on the wire** (see §6). "Version" below
refers only to this document's revision.

---

## 1. Frame structure

All multi-byte integers are **big-endian** (network order). Signed integers are
two's complement.

```
Offset  Size  Field         Notes
------  ----  ------------  ----------------------------------------------------
0       2     SYNC          0xAA55  (bytes: AA 55)   -- covered by CRC
2       1     NODE_ID       1..254
3       2     SEQ_NUM       uint16, monotonic per node, wraps 0xFFFF -> 0x0000
5       1     PAYLOAD_LEN   number of bytes in PAYLOAD (0..255)
6       N     PAYLOAD       N == PAYLOAD_LEN bytes of concatenated TLV records
6+N     2     CRC16         CRC-16/CCITT-FALSE over offsets 0 .. (6+N-1)
```

- Header struct format: `>HBHB` (6 bytes).
- **Minimum frame size:** 8 bytes (empty payload).
- **Maximum frame size:** 6 + 255 + 2 = 263 bytes.
- **CRC coverage:** the entire frame **except the 2 CRC bytes themselves** — i.e.
  `SYNC + header + payload`. Unlike many designs, `SYNC` **is** included in the
  CRC here, matching the production firmware.

### Stream framing (receiver side)

The serial link is a raw byte stream. `protocol.extract_frames(buffer)` (a
generator, lifted from `base_rx.py`) does the reassembly:

1. Scans the RX buffer for `SYNC` (`AA 55`); discards bytes before it (keeps a
   lone trailing `AA` in case a `SYNC` is split across two reads).
2. Waits until at least 6 bytes (the header) are buffered; reads `PAYLOAD_LEN`
   from offset 5.
3. Waits until `6 + PAYLOAD_LEN + 2` bytes are buffered; that slice is one
   candidate frame, yielded to the caller and removed from the buffer.
4. Caller passes each candidate frame to `parse_frame`.
5. `parse_frame` returns `crc_ok=False` (not an exception) on a bad checksum, so
   the caller can still log every candidate frame — CRC pass **and** fail — to
   `raw_frames`.

---

## 2. CRC-16

**Algorithm:** CRC-16/CCITT-FALSE (identical to `calculate_crc16()` in the
original `node_tx.py` / `base_rx.py`).

| Parameter        | Value   |
|------------------|---------|
| Polynomial       | `0x1021` |
| Initial value    | `0xFFFF` |
| Input reflected  | no      |
| Output reflected | no      |
| Final XOR        | `0x0000` |
| Check (`"123456789"`) | `0x29B1` |

Transmitted as a 2-byte big-endian value appended after the payload.

---

## 3. TLV payload

The payload is zero or more TLV records:

```
TAG (1 byte) | LEN (1 byte) | VALUE (LEN bytes)
```

- **Unknown tags MUST be skipped** using `LEN` (forward compatibility).
- A record whose `LEN` does not match the expected length for a known tag is
  skipped (treated as unknown).
- Duplicate tags: last occurrence wins.
- The encoder emits ToF (`0x01`) before IMU (`0x02`); decoders must not rely on
  ordering.

### Tag table

| Tag    | Name           | Len | Value encoding                          | Meaning                                  |
|--------|----------------|-----|-----------------------------------------|------------------------------------------|
| `0x01` | `TAG_TOF`      | 2   | `uint16`                                | ToF distance, millimetres                |
| `0x02` | `TAG_IMU_6AXIS`| 12  | `6 x int16` = `ax, ay, az, gx, gy, gz`  | accelerometer + gyroscope, fixed-point   |

### Fixed-point scaling (tag `0x02`)

| Axis group | Wire value                     | Decoded value            | Physical unit |
|------------|--------------------------------|--------------------------|---------------|
| accel `ax,ay,az` | `int(value_mss * 100)`   | `wire / 100`             | m/s^2         |
| gyro  `gx,gy,gz` | `int(value_rads * 1000)` | `wire / 1000`            | rad/s         |

The encoder **truncates toward zero** (`int(...)`, not round-half) to match the
production firmware exactly. Each scaled axis must fit in `int16`
(`-32768..32767`); the driver wrappers are responsible for staying in range.

### Decoded value keys

These key names are used everywhere downstream — on the event bus, in the
`readings` table, and in WebSocket JSON messages:

| Key         | Type              | From tag       | Unit    |
|-------------|-------------------|----------------|---------|
| `tof_mm`    | int               | `TAG_TOF`      | mm      |
| `accel_mss` | `[x, y, z]` float | `TAG_IMU_6AXIS`| m/s^2   |
| `gyro_rads` | `[x, y, z]` float | `TAG_IMU_6AXIS`| rad/s   |

`accel_mss` and `gyro_rads` always appear together (one `0x02` record carries
both). When encoding, if only one is supplied the other is packed as zeros.

---

## 4. Decoded frame object

`parse_frame(bytes) -> DecodedFrame` (a `namedtuple`):

| Field     | Type   | Notes                                                |
|-----------|--------|------------------------------------------------------|
| `node_id` | int    | from header                                          |
| `seq_num` | int    | from header                                          |
| `crc_ok`  | bool   | computed CRC == received CRC                         |
| `raw`     | bytes  | the exact candidate frame bytes (for `raw_frames`)   |
| `values`  | dict   | decoded keys above; **empty `{}` when `crc_ok` is False** |

`parse_frame` raises `ProtocolError` only for **structural** problems (bad sync,
length mismatch). A CRC failure is **not** an exception — it returns a
`DecodedFrame` with `crc_ok=False` so the caller can still log it forensically.

---

## 5. Module API (`edge/protocol.py` == `backend/ingest/protocol.py`)

| Function | Purpose |
|----------|---------|
| `crc16(data, crc=0xFFFF)` | CRC-16/CCITT-FALSE; check value `0x29B1` |
| `encode_tlv(values) -> bytes` | decoded-key dict → TLV payload bytes |
| `decode_tlv(payload) -> dict` | TLV payload bytes → decoded-key dict |
| `build_frame(node_id, seq_num, values) -> bytes` | full `SYNC..CRC` packet |
| `parse_frame(frame) -> DecodedFrame` | one candidate packet → namedtuple (`crc_ok`, `values`, …) |
| `extract_frames(buffer: bytearray)` | generator: yields complete candidate frames from a growing RX buffer, consuming them in place |

Origins: encoder = inline `struct.pack` + `calculate_crc16` in `edge/node_tx.py`
(pre-refactor); decoder + `extract_frames` = `parse_tlv_payload` and the framing
loop in `backend/legacy/base_rx.py`. `edge/protocol.py`'s `__main__` asserts its
output is byte-identical to the original inline construction.

---

## 6. Known limitations / deferred

- **No version field on the wire.** A future revision that needs one must change
  `SYNC` (e.g. `0xAA56`) or steal a bit elsewhere — a bare added byte is
  ambiguous against deployed nodes.
- No per-node timestamp in the frame; ingestion timestamps on receipt.
- No battery / uptime / temperature telemetry yet.
- `seq_num` is 16-bit and wraps roughly every 9 hours at 2 Hz; the
  `(node_id, seq_num)` uniqueness in `readings` assumes re-ingestion windows
  shorter than one wrap.

---

## 7. Change log

| Doc rev | Date       | Change                                                        |
|---------|------------|--------------------------------------------------------------|
| 1       | 2026-08-30 | Initial spec — documents the shipped `0xAA55` frame format.  |
