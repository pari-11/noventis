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

### `NODE_ID` vs. node name

`NODE_ID` is the **fixed, protocol-level identifier** for a node. It is set in
the node's firmware when it is flashed and is present in every frame header. The
backend never auto-assigns, reassigns, or rewrites it: the first frame with a
given `NODE_ID` creates that node's row (keyed by `NODE_ID`), and it is what the
`(node_id, seq_num)` uniqueness constraint on `readings` and **all**
WebSocket / REST `node_id=` filtering key off.

The **node name** (`nodes.name`, set via `PATCH /nodes/{node_id}`) is a purely
cosmetic, operator-editable label. It has **no** bearing on frame routing, the
`(node_id, seq_num)` constraint, or any API / WebSocket filtering — it exists
only so the dashboard can show something friendlier than a bare number.

Keeping `NODE_ID` unique across physical devices is the **operator's
responsibility**, handled when flashing each node's firmware (see §6).

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
| `0x10` | `TAG_CMD`      | 14  | `u8 node_id, u8 action, u32 counter, 8-byte mac` | **control**, base -> node (section 8) |
| `0x11` | `TAG_CMD_ACK`  | 5   | `u8 action, u32 counter`                | **control**, node -> base (section 8)    |

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

| Key                | Type              | From tag       | Unit    |
|--------------------|-------------------|----------------|---------|
| `tof_mm`           | int               | `TAG_TOF`      | mm      |
| `tof_out_of_range` | bool              | `TAG_TOF`      | —       |
| `accel_mss`        | `[x, y, z]` float | `TAG_IMU_6AXIS`| m/s^2   |
| `gyro_rads`        | `[x, y, z]` float | `TAG_IMU_6AXIS`| rad/s   |
| `cmd`              | dict `{node_id, action, counter, mac}` | `TAG_CMD` | control, section 8 |
| `cmd_ack`          | dict `{action, counter}` | `TAG_CMD_ACK` | control, section 8 |

`accel_mss` and `gyro_rads` always appear together (one `0x02` record carries
both). When encoding, if only one is supplied the other is packed as zeros.

`tof_out_of_range` is **not produced by the codec** and is **not on the wire** —
`decode_tlv` returns only the raw `tof_mm`. It is derived one hop downstream, in
`backend/ingest/serial_reader.py`, and added to `values` alongside the untouched
`tof_mm` whenever a ToF reading is present: `tof_mm > TOF_MAX_VALID_MM` (default
2000 mm, env `NOVENTIS_TOF_MAX_VALID_MM`). The VL53L0X emits a fixed ~8190–8191 mm
sentinel when it has no valid target; this flag lets consumers tell that apart
from a real distance without changing the frame format or the two `protocol.py`
modules. It rides through to the event bus, the `readings.tof_out_of_range`
column, and `/live` / `/readings` JSON.

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
- **Firmware `NODE_ID` collisions are not detected.** If two physical nodes are
  flashed with the same `NODE_ID`, the backend folds them into one node row:
  their frames interleave under one identity, `raw_frames` still logs every
  packet, but `readings` and the live stream become an unusable mix (and the
  `seq_num`-restart heuristic in `backend/db/writer.py` will likely thrash,
  repeatedly clearing that node's readings). Uniqueness is the operator's
  responsibility at flash time; a future wire revision could add a hardware
  UID / boot nonce to let the backend flag this.
- `seq_num` is 16-bit and wraps roughly every 9 hours at 2 Hz; the
  `(node_id, seq_num)` uniqueness in `readings` assumes re-ingestion windows
  shorter than one wrap. A **node reboot** resets `seq_num` to ~0 mid-wrap, which
  would collide with the just-stored session and be dropped by the `ON CONFLICT
  DO NOTHING` guard. `backend/db/writer.py` mitigates this: a large backward
  `seq_num` jump (distinguished from the `0xFFFF -> 0x0000` wrap) after a gap of
  silence is treated as a restart and clears that node's prior `readings`
  (`raw_frames` is kept). A future wire revision could instead carry a boot/epoch
  counter -- see the version-field note above.

---

## 7. Change log

| Doc rev | Date       | Change                                                        |
|---------|------------|--------------------------------------------------------------|
| 1       | 2026-08-30 | Initial spec — documents the shipped `0xAA55` frame format.  |
| 2       | 2026-08-31 | §3: document the derived `tof_out_of_range` key (backend-side, not on the wire; codec and both `protocol.py` modules unchanged). |
| 3       | 2026-09-01 | §1/§6: document the `NODE_ID` (firmware-fixed, routing key) vs. node `name` (cosmetic label) invariant and that firmware `NODE_ID` collisions are not detected. Docs only — no wire or behaviour change. |
| 4       | 2026-10-05 | §3 + new §8: control tags `0x10` (command, base -> node) and `0x11` (acknowledgement, node -> base) for remote node shutdown over LoRa, authenticated with a truncated HMAC-SHA256 and a replay counter. Telemetry tags and framing unchanged; older decoders skip the new tags. |

---

## 8. Control frames: remote shutdown (base -> node)

Control frames use the **same framing, CRC and node addressing** as telemetry
(section 1). They carry no sensor data and every consumer must keep them out of
readings, charts and the archive (the backend marks them `control: true`; they
remain visible in the raw packet view).

### 8.1 Command (`TAG_CMD` 0x10, base -> node)

```
TAG 0x10 | LEN 14 | node_id u8 | action u8 | counter u32 (big-endian) | mac 8 bytes
```

- Frame header `NODE_ID` = the target node, `SEQ_NUM` = 0 (the base has no
  telemetry sequence). `node_id` in the record is the target again and is what is
  authenticated.
- `action`: `0x01` = shut down (power off safely). No other actions are defined.
- `mac` = first 8 bytes of **HMAC-SHA256(secret, node_id | action | counter)**, the
  three fields packed as `>BBI` (6 bytes). `secret` is a shared string (>= 8
  characters) configured on both sides and never sent over the air.
- `counter` must be **strictly greater** than the last counter the node accepted
  (the node persists it, so this survives reboots). The backend uses wall-clock
  seconds, so the sequence only ever increases and no state file is needed on the
  base. Retries of one request reuse the same counter.

A node acts on a command only if all of: CRC valid; `node_id` is its own; `action`
is known; `mac` verifies; `counter` is newer. Anything else is silently ignored.

### 8.2 Acknowledgement (`TAG_CMD_ACK` 0x11, node -> base)

```
TAG 0x11 | LEN 5 | action u8 | counter u32
```

Sent as an ordinary frame with the node's `NODE_ID` and its **last telemetry
`SEQ_NUM`** (so no gap appears in the telemetry sequence). Sent 3 times, ~0.3 s
apart (the radio is half duplex and one may be lost), then the node waits ~2 s so
the last copy leaves the radio, then powers off. The node checks it *can* power
off before acknowledging.

### 8.3 Timing (stop-and-wait)

The radio cannot receive while it transmits, in either direction, so the base does
not blast the command: it sends once, stays quiet ~1 s to listen for the ACK, and
repeats for ~10 s. The node listens in 50 ms steps inside its normal 0.5 s idle
period between telemetry frames.

### 8.4 Outcomes the base can report

| outcome | meaning |
|---|---|
| `acked` | the node confirmed; it is powering off |
| `silent` | no ACK, and no telemetry from the node for ~3 s -- probably off, unconfirmed (may also have been off / out of range already) |
| `not_received` | no ACK and the node is still transmitting -- the command did not get through |

### 8.5 Security properties and limits

- **Authenticated, not encrypted.** A device without the secret cannot make a
  valid command, and a recorded command cannot be replayed (stale counter).
  Anyone can still *read* the traffic.
- The **ACK is not authenticated** (it only drives a status message; spoofing it
  cannot shut anything down).
- 8-byte MAC = 64 bits: ample against guessing at LoRa speeds.
- If the base's clock is ever set far into the future, commands issued afterwards
  are refused until real time catches up with the last accepted counter. Recovery:
  delete the node's counter file.
- A powered-off Pi cannot be restarted remotely; power must be re-applied.
