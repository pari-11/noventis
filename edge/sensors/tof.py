"""
edge/sensors/tof.py -- Time-of-Flight distance sensor wrapper.

Responsibility: own the ToF sensor and expose a tiny, board-agnostic read API.
Right now node_tx.py talks to `adafruit_vl53l0x` directly; this wrapper is where
that should move so the transmit loop stops importing the vendor driver.

Expected API (to be consumed by node_tx.py):
    init() -> None
    read_mm() -> int          # distance in millimetres -> protocol key "tof_mm"

Maps to protocol tag TAG_TOF (0x01), a uint16 millimetre value. There is no
separate status tag in the current wire protocol (see docs/protocol-spec.md).

TODO:
  - [ ] Move the VL53L0X init + `vl53.range` read out of node_tx.py to here.
  - [ ] init(): I2C bus, measurement_timing_budget (node_tx uses 33000).
  - [ ] Decide how to represent "no reading" (node_tx currently drops the TLV).
"""


def init():
    raise NotImplementedError("tof.init(): move the VL53L0X setup here from node_tx.py")


def read_mm():
    raise NotImplementedError("tof.read_mm(): return vl53.range (distance in mm)")
