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
"""

import time
import serial
import board
import busio
import adafruit_vl53l0x
import RPi.GPIO as GPIO

# Shared TLV + CRC-16 codec (was inline in this file).
from protocol import build_frame

# Optional IMU import
try:
    import adafruit_mpu6050
    IMU_DRIVER_AVAILABLE = True
except ImportError:
    IMU_DRIVER_AVAILABLE = False

# --- Hardware Configuration ---
UART_PORT = "/dev/serial0"
BAUD_RATE = 9600
AUX_PIN = 25
NODE_ID = 0x01

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

# 1. Initialize ToF Sensor
vl53 = adafruit_vl53l0x.VL53L0X(i2c)
vl53.measurement_timing_budget = 33000

# 2. Attempt IMU Initialization (Graceful Fallback)
imu = None
if IMU_DRIVER_AVAILABLE:
    try:
        imu = adafruit_mpu6050.MPU6050(i2c, address=0x68)
        print("[INIT] MPU-9250 IMU detected at 0x68.")
    except Exception:
        print("[INIT] MPU-9250 not detected. Running in ToF-only mode.")

ser = serial.Serial(UART_PORT, baudrate=BAUD_RATE, timeout=0.5)
print(f"Node {NODE_ID} online. Broadcasting telemetry frames...\n")

seq_num = 0

try:
    while True:
        readings = {}

        # --- TLV Tag 0x01: ToF Sensor ---
        try:
            distance_mm = vl53.range
            readings["tof_mm"] = distance_mm
        except Exception as e:
            distance_mm = None
            print(f"ToF Read Error: {e}")

        # --- TLV Tag 0x02: IMU 6-Axis (Accel + Gyro) ---
        if imu is not None:
            try:
                accel_x, accel_y, accel_z = imu.acceleration  # m/s^2
                gyro_x, gyro_y, gyro_z = imu.gyro  # rad/s
                readings["accel_mss"] = (accel_x, accel_y, accel_z)
                readings["gyro_rads"] = (gyro_x, gyro_y, gyro_z)
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
        if imu is not None:
            log_str += f" | Accel-Z: {accel_z:5.2f} m/s²"
        print(log_str)

        seq_num = (seq_num + 1) & 0xFFFF
        time.sleep(0.5)

except KeyboardInterrupt:
    print("\nTransmission stopped by user.")
finally:
    ser.close()
    GPIO.cleanup()
