import time
import struct
import serial
import board
import busio
import adafruit_vl53l0x
import RPi.GPIO as GPIO

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

# --- CRC-16 CCITT Calculation ---
def calculate_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc

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
        payload = bytearray()

        # --- TLV Tag 0x01: ToF Sensor ---
        try:
            distance_mm = vl53.range
            payload.extend(struct.pack(">BBH", 0x01, 2, distance_mm))
        except Exception as e:
            distance_mm = None
            print(f"ToF Read Error: {e}")

        # --- TLV Tag 0x02: IMU 6-Axis (Accel + Gyro) ---
        if imu is not None:
            try:
                accel_x, accel_y, accel_z = imu.acceleration  # m/s^2
                gyro_x, gyro_y, gyro_z = imu.gyro  # rad/s

                # Scale to fixed-point int16
                ax_i = int(accel_x * 100)
                ay_i = int(accel_y * 100)
                az_i = int(accel_z * 100)
                gx_i = int(gyro_x * 1000)
                gy_i = int(gyro_y * 1000)
                gz_i = int(gyro_z * 1000)

                payload.extend(struct.pack(">BBhhhhhh", 0x02, 12, ax_i, ay_i, az_i, gx_i, gy_i, gz_i))
            except Exception as e:
                print(f"IMU Read Error: {e}")

        # --- Frame Header Construction ---
        # Sync(0xAA55), Node_ID(1B), Seq(2B), Payload_Len(1B)
        header = struct.pack(">HBHB", 0xAA55, NODE_ID, seq_num, len(payload))

        # --- CRC-16 & Packet Assembly ---
        raw_frame = header + bytes(payload)
        crc16 = calculate_crc16(raw_frame)
        packet = raw_frame + struct.pack(">H", crc16)

        # --- Radio Transmission ---
        wait_for_radio_idle()
        ser.write(packet)
        ser.flush()

        log_str = f"TX Seq #{seq_num:05d} | Frame Len: {len(packet):02d}B | CRC: 0x{crc16:04X}"
        if distance_mm is not None:
            log_str += f" | ToF: {distance_mm:4d} mm"
        if imu is not None:
            log_str += f" | Accel-Z: {accel_z:5.2f} m/s\u00b2"
        print(log_str)

        seq_num = (seq_num + 1) & 0xFFFF
        time.sleep(0.5)

except KeyboardInterrupt:
    print("\nTransmission stopped by user.")
finally:
    ser.close()
    GPIO.cleanup()
