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
SYNC_BYTES = b"\xaa\x55"                   # SYNC as it appears on the wire
_HEADER_FMT = ">HBHB"                      # SYNC, NODE_ID, SEQ_NUM, PAYLOAD_LEN
HEADER_LEN = struct.calcsize(_HEADER_FMT)  # 6
CRC_LEN = 2
MIN_FRAME_LEN = HEADER_LEN + CRC_LEN       # 8
MAX_PAYLOAD_LEN = 255                      # PAYLOAD_LEN is a single byte

# ---- TLV tags (see docs/protocol-spec.md section 3) -------------------------
TAG_TOF = 0x01         # LEN 2:  uint16 distance, millimetres
TAG_IMU_6AXIS = 0x02   # LEN 12: 6x int16  ax,ay,az (accel), gx,gy,gz (gyro)
# Control tags (spec section 8) -- NOT telemetry. CMD travels base -> node,
# CMD_ACK node -> base. Consumers must keep them out of readings/charts/archive.
TAG_CMD = 0x10         # LEN 14: node_id u8, action u8, counter u32, mac 8 bytes
TAG_CMD_ACK = 0x11     # LEN 5:  action u8, counter u32

ACTION_SHUTDOWN = 0x01  # the only action defined so far

_TOF_VALUE_FMT = ">H"
_IMU_VALUE_FMT = ">hhhhhh"
_TOF_LEN = struct.calcsize(_TOF_VALUE_FMT)   # 2
_IMU_LEN = struct.calcsize(_IMU_VALUE_FMT)   # 12
_CMD_BODY_FMT = ">BBI"                       # node_id, action, counter (the MAC'd part)
_CMD_BODY_LEN = struct.calcsize(_CMD_BODY_FMT)   # 6
MAC_LEN = 8                                  # HMAC-SHA256 truncated to 8 bytes
_CMD_LEN = _CMD_BODY_LEN + MAC_LEN           # 14
_ACK_VALUE_FMT = ">BI"                       # action, counter
_ACK_LEN = struct.calcsize(_ACK_VALUE_FMT)   # 5

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
      "cmd"       -> (node_id, action, counter, mac8)      (tag 0x10, control)
      "cmd_ack"   -> (action, counter)                     (tag 0x11, control)

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

    cmd = values.get("cmd")
    if cmd is not None:    # (node_id, action, counter, mac) -- see build_command_frame
        node_id, action, counter, mac = cmd
        out += struct.pack(">BB", TAG_CMD, _CMD_LEN)
        out += struct.pack(_CMD_BODY_FMT, node_id, action, counter) + bytes(mac)

    ack = values.get("cmd_ack")
    if ack is not None:    # (action, counter)
        out += struct.pack(">BB", TAG_CMD_ACK, _ACK_LEN)
        out += struct.pack(_ACK_VALUE_FMT, ack[0], ack[1])
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
        elif tag == TAG_CMD and length == _CMD_LEN:
            c_node, c_action, c_counter = struct.unpack(_CMD_BODY_FMT, chunk[:_CMD_BODY_LEN])
            values["cmd"] = {
                "node_id": c_node, "action": c_action,
                "counter": c_counter, "mac": chunk[_CMD_BODY_LEN:],
            }
        elif tag == TAG_CMD_ACK and length == _ACK_LEN:
            a_action, a_counter = struct.unpack(_ACK_VALUE_FMT, chunk)
            values["cmd_ack"] = {"action": a_action, "counter": a_counter}
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


def _hmac_sha256(key, msg):
    """HMAC-SHA256 (RFC 2104) via hashlib only -- the `hmac` module is absent on
    MicroPython. Imported lazily so this module still loads where hashlib is not."""
    import hashlib
    key = bytes(key)
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key + b"\x00" * (64 - len(key))
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in key) + bytes(msg)).digest()
    return hashlib.sha256(bytes(b ^ 0x5C for b in key) + inner).digest()


def command_mac(secret, node_id, action, counter):
    """8-byte authentication code over (node_id, action, counter)."""
    body = struct.pack(_CMD_BODY_FMT, node_id & 0xFF, action & 0xFF, counter & 0xFFFFFFFF)
    return _hmac_sha256(secret, body)[:MAC_LEN]


def verify_command(secret, cmd):
    """True when `cmd` (the decoded "cmd" dict) carries a valid code for `secret`.

    Authenticates only. The caller must ALSO check cmd["node_id"] is its own and
    cmd["counter"] is greater than the last one it accepted (replay protection).
    """
    want = command_mac(secret, cmd["node_id"], cmd["action"], cmd["counter"])
    got = bytes(cmd["mac"])
    if len(got) != len(want):
        return False
    diff = 0
    for a, b in zip(want, got):   # constant-time compare
        diff |= a ^ b
    return diff == 0


def build_command_frame(secret, node_id, action, counter):
    """Base -> node control frame addressed to `node_id` (header NODE_ID = target,
    SEQ_NUM 0 -- the base has no telemetry sequence)."""
    mac = command_mac(secret, node_id, action, counter)
    return build_frame(node_id, 0, {"cmd": (node_id, action, counter, mac)})


def build_ack_frame(node_id, seq_num, action, counter):
    """Node -> base acknowledgement. Reuse the node's LAST telemetry seq_num so no
    gap appears in the telemetry sequence."""
    return build_frame(node_id, seq_num, {"cmd_ack": (action, counter)})


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


def extract_frames(buffer):
    """Pull complete candidate frames out of a growing RX byte buffer.

    `buffer` is a bytearray the caller keeps appending serial bytes to. For every
    complete SYNC..CRC slice this removes those bytes from the front of `buffer`
    and yields them (as `bytes`); leading garbage is dropped in place and an
    incomplete trailing frame is left buffered for the next call.

    CRC is NOT checked here -- feed each yielded slice to parse_frame(), which
    reports crc_ok. This is the resync loop from backend/legacy/base_rx.py,
    lifted into the shared module.
    """
    while True:
        i = buffer.find(SYNC_BYTES)
        if i == -1:
            # No SYNC in view. Keep a trailing 0xAA that might be the first
            # half of a SYNC split across two reads; drop everything else.
            keep_tail = buffer[-1:] == SYNC_BYTES[:1]
            del buffer[:-1 if keep_tail else len(buffer)]
            return
        if i:
            del buffer[:i]
        if len(buffer) < HEADER_LEN:
            return  # header still arriving
        total = HEADER_LEN + buffer[5] + CRC_LEN  # buffer[5] == PAYLOAD_LEN
        if len(buffer) < total:
            return  # payload + CRC still arriving
        frame = bytes(buffer[:total])
        del buffer[:total]
        yield frame


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

    # ToF-only frame (IMU absent) still round-trips.
    _tof_only = parse_frame(build_frame(2, 1, {"tof_mm": 1500}))
    assert _tof_only.crc_ok and _tof_only.values == {"tof_mm": 1500}, _tof_only

    # Stream de-framing: garbage + two frames + a partial third.
    _f2 = build_frame(1, 43, {"tof_mm": 900})
    _buf = bytearray(b"\x00\x13\xffnoise" + _ours + _f2 + _f2[:5])
    _got = list(extract_frames(_buf))
    assert _got == [_ours, _f2], [f.hex() for f in _got]
    assert bytes(_buf) == _f2[:5], bytes(_buf)  # partial frame stays buffered

    # A CRC-corrupted frame is still returned by extract_frames (parse_frame
    # then reports crc_ok=False so it can be logged to raw_frames).
    _bad = bytearray(_ours)
    _bad[-1] ^= 0xFF
    _bd = parse_frame(bytes(_bad))
    assert _bd.crc_ok is False and _bd.values == {}, _bd

    # Control frames: command + ack round-trip; MAC accepts the right secret only.
    _secret = b"correct horse battery staple"
    _cf = parse_frame(build_command_frame(_secret, 1, ACTION_SHUTDOWN, 1234567))
    assert _cf.crc_ok and _cf.node_id == 1 and _cf.seq_num == 0, _cf
    _c = _cf.values["cmd"]
    assert (_c["node_id"], _c["action"], _c["counter"]) == (1, ACTION_SHUTDOWN, 1234567), _c
    assert verify_command(_secret, _c) is True
    assert verify_command(b"wrong secret", _c) is False
    _tampered = dict(_c)
    _tampered["counter"] += 1                      # a replay with a bumped counter
    assert verify_command(_secret, _tampered) is False
    _af = parse_frame(build_ack_frame(1, 77, ACTION_SHUTDOWN, 1234567))
    assert _af.crc_ok and _af.seq_num == 77, _af
    assert _af.values == {"cmd_ack": {"action": ACTION_SHUTDOWN, "counter": 1234567}}, _af.values

    print("protocol.py self-test OK (byte-compatible with legacy):", _d.values)
