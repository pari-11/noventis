"""
edge/sensors/imu.py -- Inertial Measurement Unit driver wrapper.

Responsibility: own the IMU and expose a small, board-agnostic read API to
`node_tx.py`. Vendor driver stays behind this wrapper.

Expected API (consumed by node_tx.py), all integers, ready for TLV packing:
    init() -> None
    read_accel_mg()  -> [x, y, z]   # acceleration, milli-g          (TAG_IMU_ACCEL_MG  0x20)
    read_gyro_cdps() -> [x, y, z]   # angular rate, centi-deg/second  (TAG_IMU_GYRO_CDPS 0x21)
    read_temp_cc()   -> int         # die temperature, centi-deg C    (TAG_IMU_TEMP_CC   0x22)

Values are int16 on the wire -- callers/driver must keep them in [-32768, 32767].

TODO:
  - [ ] Confirm the IMU part (e.g. LSM6DS3 / MPU-6050 / ICM-20948) and driver lib.
  - [ ] init(): I2C/SPI setup, output data rate, full-scale ranges.
  - [ ] Convert raw LSBs -> milli-g / centi-deg-per-s here (not in node_tx.py).
  - [ ] Extract from the production script if that conversion already exists there.
"""


def init():
    raise NotImplementedError("imu.init(): wire up the real IMU driver")


def read_accel_mg():
    raise NotImplementedError("imu.read_accel_mg(): return [x, y, z] in milli-g")


def read_gyro_cdps():
    raise NotImplementedError("imu.read_gyro_cdps(): return [x, y, z] in centi-deg/s")


def read_temp_cc():
    raise NotImplementedError("imu.read_temp_cc(): return die temp in centi-deg C")
