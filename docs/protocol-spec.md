# Noventis Wire Protocol Specification

**Protocol version:** `0x01`

**Status:** This document is the **single source of truth** for the on-wire
format. Both `edge/protocol.py` and `backend/ingest/protocol.py` implement it and
**must stay byte-for-byte identical** to each other. Any change to tags or byte
layout is made here **and** in both modules, in the same commit.

---

## 1. Frame structure

All multi-byte integers are **big-endian** (network order). Signed integers are
two's complement.

```
Offset  Size  Field         Notes
------  ----  ------------  ----------------------------------------------------
0       2     SYNC          0x4E 0x56  ("NV")  -- not covered by CRC
2       1     VERSION       0x01
3       1     NODE_ID       1..254   (0x00 and 0xFF reserved / invalid)
4       2     SEQ_NUM       uint16, monotonic per node, wraps 0xFFFF -> 0x0000
6       1     PAYLOAD_LEN   number of bytes in PAYLOAD (0..255)
7       N     PAYLOAD       N == PAYLOAD_LEN bytes of concatenated TLV triples
7+N     2     CRC16         CRC-16/CCITT-FALSE over VERSION .. last PAYLOAD byte
```

- **Minimum frame size:** 9 bytes (empty payload).
- **Maximum frame size:** 7 + 255 + 2 = 264 bytes.
- **CRC coverage:** offsets `2 .. (7 + N - 1)` inclusive — i.e. everything
  between `SYNC` and `CRC16`. `SYNC` itself is **not** covered (it is only a
  stream-resynchronisation marker).

### Stream framing (receiver side)

The serial link is a raw byte stream. The reader:

1. Scans for the 2-byte `SYNC`.
2. Reads the 7-byte header, takes `PAYLOAD_LEN`.
3. Reads `PAYLOAD_LEN + 2` more bytes (payload + CRC).
4. Passes the whole candidate frame to `parse_frame`.
5. On bad sync / length / CRC: advances **one byte** past the failed `SYNC` and
   keeps scanning (resync). Every candidate frame — CRC pass **and** fail — is
   still logged to `raw_frames`.

---

## 2. CRC-16

**Algorithm:** CRC-16/CCITT-FALSE

| Parameter        | Value   |
|------------------|---------|
| Polynomial       | `0x1021` |
| Initial value    | `0xFFFF` |
| Input reflected  | no      |
| Output reflected | no      |
| Final XOR        | `0x0000` |
| Check (`"123456789"`) | `0x29B1` |

---

## 3. TLV payload

The payload is zero or more TLV triples:

```
TAG (1 byte) | LEN (1 byte) | VALUE (LEN bytes)
```

- **Unknown tags MUST be skipped** using `LEN` (forward compatibility).
- **Duplicate tags:** last occurrence wins.
- Tags are emitted by the encoder in ascending tag order for deterministic
  frames, but decoders must not rely on ordering.

### Tag table

| Tag    | Name                | Len | Value encoding      | Units / meaning                              |
|--------|---------------------|-----|---------------------|----------------------------------------------|
| `0x01` | `TAG_UPTIME_MS`     | 4   | `uint32`            | milliseconds since node boot                 |
| `0x02` | `TAG_VBAT_MV`       | 2   | `uint16`            | battery voltage, millivolts                  |
| `0x10` | `TAG_TOF_DIST_MM`   | 2   | `uint16`            | ToF distance, mm (`0xFFFF` = out of range)   |
| `0x11` | `TAG_TOF_STATUS`    | 1   | `uint8`             | ToF range-status code (sensor-specific)      |
| `0x20` | `TAG_IMU_ACCEL_MG`  | 6   | `3 x int16` (x,y,z) | acceleration, milli-g                        |
| `0x21` | `TAG_IMU_GYRO_CDPS` | 6   | `3 x int16` (x,y,z) | angular rate, centi-degrees/second           |
| `0x22` | `TAG_IMU_TEMP_CC`   | 2   | `int16`             | IMU die temperature, centi-degrees Celsius   |

### Decoded value keys

These key names are used everywhere downstream — on the event bus, in the
`readings` table, and in WebSocket JSON messages:

| Key            | Type        | From tag              |
|----------------|-------------|-----------------------|
| `uptime_ms`    | int         | `TAG_UPTIME_MS`       |
| `vbat_mv`      | int         | `TAG_VBAT_MV`         |
| `tof_dist_mm`  | int         | `TAG_TOF_DIST_MM`     |
| `tof_status`   | int         | `TAG_TOF_STATUS`      |
| `accel_mg`     | `[x, y, z]` | `TAG_IMU_ACCEL_MG`    |
| `gyro_cdps`    | `[x, y, z]` | `TAG_IMU_GYRO_CDPS`   |
| `imu_temp_cc`  | int         | `TAG_IMU_TEMP_CC`     |

---

## 4. Decoded frame object

`parse_frame(bytes) -> DecodedFrame` (a `namedtuple`):

| Field     | Type   | Notes                                                |
|-----------|--------|------------------------------------------------------|
| `node_id` | int    | from header                                          |
| `seq_num` | int    | from header                                          |
| `version` | int    | from header                                          |
| `crc_ok`  | bool   | computed CRC == received CRC                         |
| `raw`     | bytes  | the exact candidate frame bytes (for `raw_frames`)   |
| `values`  | dict   | decoded keys above; **empty `{}` when `crc_ok` is False** |

`parse_frame` raises `ProtocolError` only for **structural** problems (bad sync,
length mismatch). A CRC failure is **not** an exception — it returns a
`DecodedFrame` with `crc_ok=False` so the caller can still log it forensically.

---

## 5. Change log

| Version | Date       | Change            |
|---------|------------|-------------------|
| `0x01`  | 2026-08-30 | Initial spec.     |
