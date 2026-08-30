"""
Noventis wire protocol -- TLV + CRC-16 codec.

SOURCE OF TRUTH: docs/protocol-spec.md
MIRROR: edge/protocol.py and backend/ingest/protocol.py MUST stay byte-for-byte
identical (tag values, byte layout, CRC parameters). If they drift, ingestion
silently corrupts data. Change docs/protocol-spec.md AND both modules together,
in one commit. This is the project's single point of failure -- treat it as such.

Kept dependency-free and MicroPython-safe on purpose: standard library only, no
dataclasses, no typing imports. Please do not add them.
"""

import struct

try:
    from collections import namedtuple
except ImportError:  # MicroPython
    from ucollections import namedtuple

PROTOCOL_VERSION = 0x01
SYNC = b"\x4e\x56"  # "NV"

# Header: SYNC(2) VERSION(1) NODE_ID(1) SEQ_NUM(2, BE) PAYLOAD_LEN(1)
_HEADER_FMT = ">2sBBHB"
HEADER_LEN = struct.calcsize(_HEADER_FMT)  # 7
CRC_LEN = 2
MIN_FRAME_LEN = HEADER_LEN + CRC_LEN       # 9
MAX_PAYLOAD_LEN = 255

# ---- TLV tags (see docs/protocol-spec.md section 3) ----------------------
TAG_UPTIME_MS     = 0x01  # uint32   ms since boot
TAG_VBAT_MV       = 0x02  # uint16   battery millivolts
TAG_TOF_DIST_MM   = 0x10  # uint16   distance mm (0xFFFF = out of range)
TAG_TOF_STATUS    = 0x11  # uint8    range status code
TAG_IMU_ACCEL_MG  = 0x20  # 3x int16 accel x,y,z (milli-g)
TAG_IMU_GYRO_CDPS = 0x21  # 3x int16 gyro x,y,z (centi-deg/s)
TAG_IMU_TEMP_CC   = 0x22  # int16    IMU die temperature (centi-degC)

# (tag, struct fmt for VALUE, decoded-dict key, is_vector)
_TAG_SPEC = (
    (TAG_UPTIME_MS,     ">I",  "uptime_ms",   False),
    (TAG_VBAT_MV,       ">H",  "vbat_mv",     False),
    (TAG_TOF_DIST_MM,   ">H",  "tof_dist_mm", False),
    (TAG_TOF_STATUS,    ">B",  "tof_status",  False),
    (TAG_IMU_ACCEL_MG,  ">3h", "accel_mg",    True),
    (TAG_IMU_GYRO_CDPS, ">3h", "gyro_cdps",   True),
    (TAG_IMU_TEMP_CC,   ">h",  "imu_temp_cc", False),
)
_BY_TAG = {tag: (fmt, key, vec) for (tag, fmt, key, vec) in _TAG_SPEC}
_BY_KEY = {key: (tag, fmt, vec) for (tag, fmt, key, vec) in _TAG_SPEC}


class ProtocolError(ValueError):
    """Raised on structural frame problems (bad sync, length). NOT on CRC fail."""


DecodedFrame = namedtuple(
    "DecodedFrame", ("node_id", "seq_num", "version", "crc_ok", "raw", "values")
)


def crc16(data, crc=0xFFFF):
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final xor.

    Sanity check: crc16(b"123456789") == 0x29B1.
    """
    for b in bytearray(data):
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def encode_tlv(values):
    """Map of decoded keys -> TLV payload bytes. Missing/None keys are skipped.

    Tags are emitted in _TAG_SPEC order so identical inputs produce identical
    frames.
    """
    out = bytearray()
    for tag, fmt, key, is_vec in _TAG_SPEC:
        if key not in values or values[key] is None:
            continue
        v = values[key]
        packed = struct.pack(fmt, *v) if is_vec else struct.pack(fmt, v)
        out.append(tag)
        out.append(len(packed))
        out.extend(packed)
    return bytes(out)


def decode_tlv(payload):
    """TLV payload bytes -> dict of decoded keys. Unknown tags skipped by length."""
    values = {}
    i = 0
    n = len(payload)
    while i + 2 <= n:
        tag = payload[i]
        length = payload[i + 1]
        i += 2
        if i + length > n:
            raise ProtocolError("truncated TLV value")
        chunk = bytes(payload[i:i + length])
        i += length
        spec = _BY_TAG.get(tag)
        if spec is None:
            continue  # unknown tag: forward-compatible skip
        fmt, key, is_vec = spec
        unpacked = struct.unpack(fmt, chunk)
        values[key] = list(unpacked) if is_vec else unpacked[0]
    return values


def build_frame(node_id, seq_num, values):
    """Build a complete on-wire frame (SYNC .. CRC16) from decoded values."""
    payload = encode_tlv(values)
    if len(payload) > MAX_PAYLOAD_LEN:
        raise ProtocolError("payload exceeds %d bytes" % MAX_PAYLOAD_LEN)
    header = struct.pack(
        _HEADER_FMT, SYNC, PROTOCOL_VERSION,
        node_id & 0xFF, seq_num & 0xFFFF, len(payload),
    )
    crc_region = header[len(SYNC):] + payload  # CRC covers everything after SYNC
    crc = crc16(crc_region)
    return header + payload + struct.pack(">H", crc)


def parse_frame(frame):
    """Parse one candidate frame -> DecodedFrame (values == {} when CRC fails).

    Raises ProtocolError only for structural problems (bad sync / wrong length),
    never for a CRC mismatch -- callers still log CRC failures to raw_frames.
    """
    frame = bytes(frame)
    if len(frame) < MIN_FRAME_LEN:
        raise ProtocolError("frame shorter than %d bytes" % MIN_FRAME_LEN)
    if frame[:len(SYNC)] != SYNC:
        raise ProtocolError("bad sync")
    _sync, version, node_id, seq_num, payload_len = struct.unpack(
        _HEADER_FMT, frame[:HEADER_LEN]
    )
    expected = HEADER_LEN + payload_len + CRC_LEN
    if len(frame) != expected:
        raise ProtocolError(
            "length mismatch: got %d expected %d" % (len(frame), expected)
        )
    payload = frame[HEADER_LEN:HEADER_LEN + payload_len]
    rx_crc = struct.unpack(">H", frame[-CRC_LEN:])[0]
    crc_ok = crc16(frame[len(SYNC):HEADER_LEN + payload_len]) == rx_crc
    values = decode_tlv(payload) if crc_ok else {}
    return DecodedFrame(node_id, seq_num, version, crc_ok, frame, values)


if __name__ == "__main__":
    # Smoke test -- runnable on CPython or MicroPython.
    assert crc16(b"123456789") == 0x29B1, "CRC self-check failed"
    _f = build_frame(7, 42, {
        "uptime_ms": 123456,
        "vbat_mv": 3987,
        "tof_dist_mm": 812,
        "accel_mg": [10, -20, 1001],
        "gyro_cdps": [0, 1, -2],
        "imu_temp_cc": 2537,
    })
    _d = parse_frame(_f)
    assert _d.crc_ok and _d.node_id == 7 and _d.seq_num == 42, _d
    assert _d.values["accel_mg"] == [10, -20, 1001], _d.values
    print("protocol.py self-test OK:", _d.values)
