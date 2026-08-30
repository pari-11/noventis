"""
edge/sensors/tof.py -- Time-of-Flight distance sensor driver wrapper.

Responsibility: own the ToF sensor and expose a tiny, board-agnostic read API
that `node_tx.py` calls. Keep the actual vendor driver behind this wrapper so the
transmit loop never imports it directly.

Expected API (consumed by node_tx.py):
    init() -> None
    read_mm() -> int          # distance in millimetres; 0xFFFF (65535) = out of range
    read_status() -> int      # sensor range-status code (0 == valid)

These map to protocol tags TAG_TOF_DIST_MM (0x10) and TAG_TOF_STATUS (0x11).

TODO:
  - [ ] Pick / confirm the sensor part (e.g. VL53L0X / VL53L1X) and driver lib.
  - [ ] init(): configure I2C bus, timing budget, continuous vs single-shot.
  - [ ] read_mm(): clamp / map "out of range" to 0xFFFF per docs/protocol-spec.md.
  - [ ] Extract this from the production script if the logic already lives there.
"""

RANGE_INVALID_MM = 0xFFFF


def init():
    raise NotImplementedError("tof.init(): wire up the real ToF driver")


def read_mm():
    raise NotImplementedError("tof.read_mm(): return distance in mm (0xFFFF if invalid)")


def read_status():
    raise NotImplementedError("tof.read_status(): return sensor range-status code")
