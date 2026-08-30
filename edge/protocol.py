"""
Noventis wire protocol -- TLV framing + CRC-16 codec.

SOURCE OF TRUTH: docs/protocol-spec.md
MIRROR: edge/protocol.py and backend/ingest/protocol.py MUST stay byte-for-byte
identical (tag values, byte layout, CRC parameters, scale factors). If they
drift, ingestion silently corrupts data. Change docs/protocol-spec.md AND both
modules together, in one commit. This is the project's single point of failure.

This module is the shared extraction of framing/CRC logic that previously lived
inline in edge/node_tx.py (encode) and backend/legacy/base_rx.py (decode). The
__main__ block asserts byte-compatibility with that original construction.

Kept dependency-free and MicroPython-safe on purpose: standard library only, no
dataclasses, no typing imports. Please do not add them.
"""

import struct

try:
    from collections import namedtuple
except ImportError:  # MicroPython
    from ucollections import namedtuple

# ---- Framing ------------------------------------------------------------------
SYNC = 0xAA55                              # header starts with this, big-endian
_HEADER_FMT = ">HBHB"                      # SYNC, NODE_ID, SEQ_NUM, PAYLOAD_LEN
HEADER_LEN = struct.calcsize(_HEADER_FMT)  # 6
CRC_LEN = 2
MIN_FRAME_LEN = HEADER_LEN + CRC_LEN       # 8
MAX_PAYLOAD_LEN = 255                      # PAYLOAD_LEN is a single byte

# ---- TLV tags (see docs/protocol-spec.md section 3) -------------------------
TAG_TOF = 0x01         # LEN 2:  uint16 distance, millimetres
TAG_IMU_6AXIS = 0x02   # LEN 12: 6x int16  ax,ay,az (accel), gx,gy,gz (gyro)

_TOF_VALUE_FMT = ">H"
_IMU_VALUE_FMT = ">hhhhhh"
_TOF_LEN = struct.calcsize(_TOF_VALUE_FMT)   # 2
_IMU_LEN = struct.calcsize(_IMU_VALUE_FMT)   # 12

# Fixed-point scale factors: wire = int(physical * SCALE); physical = wire / SCALE
ACCEL_SCALE = 100      # LSB per m/s^2
GYRO_SCALE = 1000      # LSB per rad/s


class ProtocolError(ValueError):
    """Structural frame problem (bad sync / length). NOT raised on CRC mismatch."""


DecodedFrame = namedtuple(
    "DecodedFrame", ("node_id", "seq_num", "crc_ok", "raw", "values")
)


def crc16(data, crc=0xFFFF):
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final xor.

    Byte-identical to calculate_crc16() in the original node_tx.py / base_rx.py.
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

    Recognised keys (see docs/protocol-spec.md section 3):
      "tof_mm"    -> int millimetres                       (tag 0x01)
      "accel_mss" -> (x, y, z) floats in m/s^2             (tag 0x02, with gyro)
      "gyro_rads" -> (x, y, z) floats in rad/s             (tag 0x02, with accel)

    accel_mss and gyro_rads share one 0x02 record; if only one is supplied the
    other is packed as zeros. Scaling truncates toward zero to match firmware.
    """
    out = bytearray()

    tof = values.get("tof_mm")
    if tof is not None:
        out += struct.pack(">BB", TAG_TOF, _TOF_LEN)
        out += struct.pack(_TOF_VALUE_FMT, int(tof))

    accel = values.get("accel_mss")
    gyro = values.get("gyro_rads")
    if accel is not None or gyro is not None:
        ax, ay, az = accel if accel is not None else (0.0, 0.0, 0.0)
        gx, gy, gz = gyro if gyro is not None else (0.0, 0.0, 0.0)
        out += struct.pack(">BB", TAG_IMU_6AXIS, _IMU_LEN)
        out += struct.pack(
            _IMU_VALUE_FMT,
            int(ax * ACCEL_SCALE), int(ay * ACCEL_SCALE), int(az * ACCEL_SCALE),
            int(gx * GYRO_SCALE), int(gy * GYRO_SCALE), int(gz * GYRO_SCALE),
        )
    return bytes(out)


def decode_tlv(payload):
    """TLV payload bytes -> dict of decoded values. Unknown/malformed tags skipped."""
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
        if tag == TAG_TOF and length == _TOF_LEN:
            values["tof_mm"] = struct.unpack(_TOF_VALUE_FMT, chunk)[0]
        elif tag == TAG_IMU_6AXIS and length == _IMU_LEN:
            ax, ay, az, gx, gy, gz = struct.unpack(_IMU_VALUE_FMT, chunk)
            values["accel_mss"] = [ax / ACCEL_SCALE, ay / ACCEL_SCALE, az / ACCEL_SCALE]
            values["gyro_rads"] = [gx / GYRO_SCALE, gy / GYRO_SCALE, gz / GYRO_SCALE]
        # else: unknown tag or unexpected length -> skip (forward-compatible)
    return values


def build_frame(node_id, seq_num, values):
    """Build a complete on-wire packet (SYNC .. CRC16) from decoded values."""
    payload = encode_tlv(values)
    if len(payload) > MAX_PAYLOAD_LEN:
        raise ProtocolError("payload exceeds %d bytes" % MAX_PAYLOAD_LEN)
    header = struct.pack(
        _HEADER_FMT, SYNC, node_id & 0xFF, seq_num & 0xFFFF, len(payload)
    )
    raw = header + payload  # CRC covers header (incl. SYNC) + payload
    return raw + struct.pack(">H", crc16(raw))


def parse_frame(frame):
    """Parse one candidate packet -> DecodedFrame (values == {} when CRC fails).

    Raises ProtocolError only for structural problems (bad sync / wrong length),
    never for a CRC mismatch -- callers still log CRC failures to raw_frames.
    """
    frame = bytes(frame)
    if len(frame) < MIN_FRAME_LEN:
        raise ProtocolError("frame shorter than %d bytes" % MIN_FRAME_LEN)
    sync, node_id, seq_num, payload_len = struct.unpack(
        _HEADER_FMT, frame[:HEADER_LEN]
    )
    if sync != SYNC:
        raise ProtocolError("bad sync 0x%04X" % sync)
    expected = HEADER_LEN + payload_len + CRC_LEN
    if len(frame) != expected:
        raise ProtocolError(
            "length mismatch: got %d expected %d" % (len(frame), expected)
        )
    raw = frame[:-CRC_LEN]
    rx_crc = struct.unpack(">H", frame[-CRC_LEN:])[0]
    crc_ok = crc16(raw) == rx_crc
    payload = raw[HEADER_LEN:]
    values = decode_tlv(payload) if crc_ok else {}
    return DecodedFrame(node_id, seq_num, crc_ok, frame, values)


if __name__ == "__main__":
    # Runs on CPython or MicroPython.
    assert crc16(b"123456789") == 0x29B1, "CRC self-check failed"

    # Byte-compatibility check against the ORIGINAL inline construction in
    # node_tx.py -- if this fails, the refactor changed the wire format.
    _node, _seq, _dist = 0x01, 42, 812
    _ax, _ay, _az, _gx, _gy, _gz = 1.23, -0.45, 9.81, 0.01, -0.02, 0.03
    _p = struct.pack(">BBH", 0x01, 2, _dist)
    _p += struct.pack(">BBhhhhhh", 0x02, 12,
                      int(_ax * 100), int(_ay * 100), int(_az * 100),
                      int(_gx * 1000), int(_gy * 1000), int(_gz * 1000))
    _hdr = struct.pack(">HBHB", 0xAA55, _node, _seq, len(_p))
    _legacy = _hdr + _p + struct.pack(">H", crc16(_hdr + _p))

    _ours = build_frame(_node, _seq, {
        "tof_mm": _dist,
        "accel_mss": (_ax, _ay, _az),
        "gyro_rads": (_gx, _gy, _gz),
    })
    assert _ours == _legacy, (_ours.hex(), _legacy.hex())

    _d = parse_frame(_ours)
    assert _d.crc_ok and _d.node_id == _node and _d.seq_num == _seq, _d
    assert _d.values["tof_mm"] == _dist, _d.values
    assert _d.values["accel_mss"] == [int(_ax * 100) / 100,
                                      int(_ay * 100) / 100,
                                      int(_az * 100) / 100], _d.values
    print("protocol.py self-test OK (byte-compatible with legacy):", _d.values)
