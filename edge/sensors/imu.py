"""
edge/sensors/imu.py -- Inertial Measurement Unit wrapper.

Responsibility: own the IMU and expose a small, board-agnostic read API. Right
now node_tx.py talks to `adafruit_mpu6050` directly (with a graceful fallback to
ToF-only mode when the driver or chip is absent); that should move here.

Expected API (to be consumed by node_tx.py):
    init() -> bool                     # True if an IMU was found (fallback-friendly)
    read_accel_mss() -> (x, y, z)      # acceleration, m/s^2   -> protocol key "accel_mss"
    read_gyro_rads() -> (x, y, z)      # angular rate, rad/s   -> protocol key "gyro_rads"

Both map to the single protocol tag TAG_IMU_6AXIS (0x02). The fixed-point
scaling (accel * 100, gyro * 1000, int16) is done in protocol.encode_tlv, NOT
here -- this wrapper returns physical floats straight from the driver
(`imu.acceleration`, `imu.gyro`). See docs/protocol-spec.md section 3.

There is no temperature tag in the current wire protocol.

TODO:
  - [ ] Move the MPU6050 init + graceful-fallback logic here from node_tx.py.
  - [ ] init(): I2C, address 0x68, return False instead of raising when absent.
  - [ ] Keep axis values within int16 after scaling (protocol asserts range).
"""


def init():
    raise NotImplementedError("imu.init(): move the MPU6050 setup + fallback here")


def read_accel_mss():
    raise NotImplementedError("imu.read_accel_mss(): return imu.acceleration (x, y, z) m/s^2")


def read_gyro_rads():
    raise NotImplementedError("imu.read_gyro_rads(): return imu.gyro (x, y, z) rad/s")
