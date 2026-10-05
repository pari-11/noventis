"""
edge/node_tx.py -- Noventis edge node transmit loop (production script).

Refactor note (project constraint #1): the sensor-read and LoRa-transmit logic
below is unchanged from the production script. The ONLY change is that the inline
TLV `struct.pack` calls, the `calculate_crc16` helper, and the header/packet
assembly have been moved into the shared `protocol` module (edge/protocol.py,
byte-for-byte identical to backend/ingest/protocol.py). See docs/protocol-spec.md.

Fixed-point scaling (accel * 100, gyro * 1000) now lives in
`protocol.encode_tlv`; `protocol.build_frame` produces byte-identical frames to
the previous inline construction (asserted by protocol.py's __main__ self-test).

IMU note: this node's board reports WHO_AM_I 0x70 (MPU6500) at I2C address
0x68 -- register-compatible with the MPU6050 for accel/gyro, just a different
chip ID. adafruit_mpu6050's CircuitPython register descriptors hit a
memoryview/int compatibility bug against this Pi's Blinka/PureIO versions, so
the IMU is read directly over smbus2 instead (DirectMPU below), bypassing that
dependency chain entirely.
"""

import os
import subprocess
import time
import struct
import serial
import board
import busio
import smbus2
import adafruit_vl53l0x
import RPi.GPIO as GPIO

# Shared TLV + CRC-16 codec (was inline in this file).
from protocol import (
    ACTION_SHUTDOWN, ProtocolError, build_ack_frame, build_frame,
    extract_frames, parse_frame, verify_command,
)

# --- Hardware Configuration ---
UART_PORT = "/dev/serial0"
BAUD_RATE = 9600
AUX_PIN = 25
NODE_ID = 0x01

# VL53L0X measurement timing budget (microseconds). This does NOT change how
# often readings are taken -- that is the transmit loop's time.sleep(0.5) below
# (~2 Hz). The timing budget only controls how much internal averaging the
# sensor does per single reading before returning a value: a longer budget means
# less noise per reading. At 100 ms the sensor still finishes well within the
# 500 ms loop interval (no contention), so this is a pure precision improvement
# -- no reduction in reading frequency and no filtering of the data itself.
# Every reading the sensor produces is still transmitted and stored as-is, so
# the frontend's "Raw" mode still reflects genuine, unfiltered sensor output.
TOF_TIMING_BUDGET_US = 100000  # 100 ms (was 33 ms)

# --- Setup GPIO & Radio Status ---
GPIO.setmode(GPIO.BCM)
GPIO.setup(AUX_PIN, GPIO.IN)

def wait_for_radio_idle(timeout=2.0):
    start = time.time()
    while GPIO.input(AUX_PIN) == GPIO.LOW:
        if time.time() - start > timeout:
            break
        time.sleep(0.005)

# --- Initialize Peripherals ---
i2c = busio.I2C(board.SCL, board.SDA)

# 1. Initialize ToF Sensor (Graceful Fallback -- mirrors the IMU handling below;
#    a wedged / unwired VL53L0X must not stop the node transmitting).
try:
    vl53 = adafruit_vl53l0x.VL53L0X(i2c)
    vl53.measurement_timing_budget = TOF_TIMING_BUDGET_US
    print("[INIT] VL53L0X ToF detected at 0x29.")
except Exception as e:
    vl53 = None
    print(f"[INIT] VL53L0X not available ({e}). Transmitting without ToF.")

# 2. Initialize IMU (direct SMBus driver -- works with 0x68/0x70/0x71/0x73:
#    MPU6050/6500/9250/9255 all share this register layout for accel/gyro).
class DirectMPU:
    def __init__(self, bus_num=1, address=0x68):
        self.address = address
        self.bus = smbus2.SMBus(bus_num)
        # Wake the sensor: clear SLEEP bit in PWR_MGMT_1 (0x6B).
        self.bus.write_byte_data(self.address, 0x6B, 0x00)
        # Force known ranges rather than trust power-on-reset defaults, since
        # a register write from an earlier run could otherwise leave these on
        # a different setting without the sensor reporting any error.
        self.bus.write_byte_data(self.address, 0x1C, 0x00)  # ACCEL_CONFIG: +/-2g
        self.bus.write_byte_data(self.address, 0x1B, 0x00)  # GYRO_CONFIG: +/-250 deg/s
        time.sleep(0.05)

    def read_motion(self):
        # Burst read 14 bytes starting at ACCEL_XOUT_H (0x3B):
        # 0x3B..0x40 Accel X,Y,Z | 0x41..0x42 Temp | 0x43..0x48 Gyro X,Y,Z
        data = self.bus.read_i2c_block_data(self.address, 0x3B, 14)
        raw = struct.unpack(">hhhhhhh", bytes(data))

        # +/-2g range: 16384 LSB/g * 9.80665 m/s^2/g
        ax = (raw[0] / 16384.0) * 9.80665
        ay = (raw[1] / 16384.0) * 9.80665
        az = (raw[2] / 16384.0) * 9.80665

        # +/-250 deg/s range: 131.0 LSB/(deg/s), converted to rad/s
        deg_to_rad = 3.141592653589793 / 180.0
        gx = (raw[4] / 131.0) * deg_to_rad
        gy = (raw[5] / 131.0) * deg_to_rad
        gz = (raw[6] / 131.0) * deg_to_rad

        return (ax, ay, az), (gx, gy, gz)

imu = None
try:
    imu = DirectMPU(bus_num=1, address=0x68)
    print("[INIT] IMU (ID: 0x70) detected and initialized at 0x68.")
except Exception as e:
    print(f"[INIT] IMU not detected ({e}). Running in ToF-only mode.")

ser = serial.Serial(UART_PORT, baudrate=BAUD_RATE, timeout=0.5)
print(f"Node {NODE_ID} online. Broadcasting telemetry frames...\n")

seq_num = 0

# --- Remote shutdown command (docs/protocol-spec.md section 8) ----------------
# Added AFTER the original sensor/transmit logic and does not change it: the only
# edit to the loop is that its closing time.sleep(0.5) became idle_and_listen(0.5),
# which waits the same 0.5 s but checks the radio for a command in between.
#
# The base station can send an authenticated "shut down" frame. It is acted on
# only if (1) its HMAC matches the secret in SECRET_FILE, (2) it is addressed to
# this NODE_ID, and (3) its counter is higher than the last one accepted
# (COUNTER_FILE) so a recorded command can never be replayed. Without a secret
# file the feature is simply off and telemetry is unaffected. Neither file is in
# git (see .gitignore).
_HERE = os.path.dirname(os.path.abspath(__file__))
SECRET_FILE = os.getenv("NOVENTIS_CMD_SECRET_FILE", os.path.join(_HERE, ".cmd_secret"))
COUNTER_FILE = os.getenv("NOVENTIS_CMD_COUNTER_FILE", os.path.join(_HERE, ".cmd_counter"))
ACK_REPEATS = 3          # the ACK is repeated: the radio is half duplex and one may be lost
ACK_GAP_S = 0.3
POWEROFF_DELAY_S = 2.0   # let the last ACK leave the radio before power drops


def _load_secret():
    try:
        with open(SECRET_FILE, "rb") as f:
            secret = f.read().strip()
        return secret if len(secret) >= 8 else None
    except OSError:
        return None


def _load_counter():
    try:
        with open(COUNTER_FILE) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _save_counter(value):
    tmp = COUNTER_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(value))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, COUNTER_FILE)


def _can_poweroff():
    """Root, or passwordless sudo -- checked BEFORE acknowledging, so we never
    tell the base 'shutting down' and then fail to."""
    return os.geteuid() == 0 or subprocess.call(["sudo", "-n", "true"]) == 0


cmd_secret = _load_secret()
last_cmd_counter = _load_counter()
cmd_rx = bytearray()
print("[INIT] Remote shutdown " + ("enabled." if cmd_secret else "disabled (no secret file)."))


def _handle_command(cmd):
    global last_cmd_counter
    if cmd["node_id"] != NODE_ID or cmd["action"] != ACTION_SHUTDOWN:
        return
    if not verify_command(cmd_secret, cmd):
        print("[CMD] shutdown rejected: bad authentication code")
        return
    if cmd["counter"] <= last_cmd_counter:
        print("[CMD] shutdown rejected: stale counter (replay?)")
        return
    if not _can_poweroff():
        print("[CMD] shutdown command valid but this user cannot power off (needs root or passwordless sudo)")
        return
    last_cmd_counter = cmd["counter"]
    _save_counter(last_cmd_counter)          # persist BEFORE acting
    print(f"[CMD] valid shutdown command (counter {last_cmd_counter}) -- acknowledging")
    ack = build_ack_frame(NODE_ID, (seq_num - 1) & 0xFFFF, cmd["action"], cmd["counter"])
    for _ in range(ACK_REPEATS):
        wait_for_radio_idle()
        ser.write(ack)
        ser.flush()
        time.sleep(ACK_GAP_S)
    time.sleep(POWEROFF_DELAY_S)
    argv = ["systemctl", "poweroff"] if os.geteuid() == 0 else ["sudo", "-n", "systemctl", "poweroff"]
    subprocess.call(argv)


def poll_command():
    """Drain the radio's receive buffer; act on a valid shutdown command. Never
    raises -- a fault here must not stop telemetry."""
    try:
        waiting = ser.in_waiting
        if waiting:
            chunk = ser.read(waiting)
            if cmd_secret is None:
                return                       # feature off: just keep the buffer empty
            cmd_rx.extend(chunk)
            if len(cmd_rx) > 1024:
                del cmd_rx[:-512]
        if cmd_secret is None:
            return
        for frame in extract_frames(cmd_rx):
            try:
                decoded = parse_frame(frame)
            except ProtocolError:
                continue
            if decoded.crc_ok and "cmd" in decoded.values:
                _handle_command(decoded.values["cmd"])
    except Exception as e:
        print(f"[CMD] listener error: {e}")


def idle_and_listen(duration, step=0.05):
    """Same wait as time.sleep(duration), but checks for a command every `step`."""
    end = time.time() + duration
    while True:
        poll_command()
        remaining = end - time.time()
        if remaining <= 0:
            break
        time.sleep(min(step, remaining))


try:
    while True:
        readings = {}
        distance_mm = None
        accel_z = None
        gyro_tuple = None

        # --- TLV Tag 0x01: ToF Sensor ---
        if vl53 is not None:
            try:
                distance_mm = vl53.range
                readings["tof_mm"] = distance_mm
            except Exception as e:
                print(f"ToF Read Error: {e}")

        # --- TLV Tag 0x02: IMU 6-Axis (Accel + Gyro) ---
        if imu is not None:
            try:
                accel_tuple, gyro_tuple = imu.read_motion()
                accel_z = accel_tuple[2]
                readings["accel_mss"] = accel_tuple
                readings["gyro_rads"] = gyro_tuple
            except Exception as e:
                print(f"IMU Read Error: {e}")

        # --- Frame encode: TLV + header + CRC-16 (shared protocol module) ---
        packet = build_frame(NODE_ID, seq_num, readings)
        crc_val = int.from_bytes(packet[-2:], "big")

        # --- Radio Transmission ---
        wait_for_radio_idle()
        ser.write(packet)
        ser.flush()

        log_str = f"TX Seq #{seq_num:05d} | Frame Len: {len(packet):02d}B | CRC: 0x{crc_val:04X}"
        if distance_mm is not None:
            log_str += f" | ToF: {distance_mm:4d} mm"
        if accel_z is not None:
            log_str += f" | Accel-Z: {accel_z:5.2f} m/s²"
        if gyro_tuple is not None:
            log_str += f" | Gyro: ({gyro_tuple[0]:5.2f}, {gyro_tuple[1]:5.2f}, {gyro_tuple[2]:5.2f}) rad/s"
        print(log_str)

        seq_num = (seq_num + 1) & 0xFFFF
        idle_and_listen(0.5)   # was time.sleep(0.5); same 0.5 s, but listens for a command

except KeyboardInterrupt:
    print("\nTransmission stopped by user.")
finally:
    ser.close()
    GPIO.cleanup()
