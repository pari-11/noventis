import serial
import struct
import time
import sqlite3
from datetime import datetime

COM_PORT = 'COM4'  # Update if required
BAUD_RATE = 9600
DB_FILE = "telemetry.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            node_id INTEGER,
            seq_num INTEGER,
            frame_len INTEGER,
            crc_status TEXT,
            tof_distance_mm INTEGER,
            accel_x REAL,
            accel_y REAL,
            accel_z REAL,
            gyro_x REAL,
            gyro_y REAL,
            gyro_z REAL
        )
    """)
    conn.commit()
    conn.close()

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

def parse_tlv_payload(payload_bytes):
    idx = 0
    parsed = {}
    while idx < len(payload_bytes):
        tag = payload_bytes[idx]
        length = payload_bytes[idx + 1]
        val_bytes = payload_bytes[idx + 2: idx + 2 + length]
        if tag == 0x01 and length == 2:
            parsed['tof_mm'] = struct.unpack(">H", val_bytes)[0]
        elif tag == 0x02 and length == 12:
            ax, ay, az, gx, gy, gz = struct.unpack(">hhhhhh", val_bytes)
            parsed['accel'] = {'x': ax / 100.0, 'y': ay / 100.0, 'z': az / 100.0}
            parsed['gyro'] = {'x': gx / 1000.0, 'y': gy / 1000.0, 'z': gz / 1000.0}
        idx += 2 + length
    return parsed

def log_to_database(node_id, seq_num, frame_len, crc_status, sensor_data):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    tof_val = sensor_data.get('tof_mm', None)
    acc = sensor_data.get('accel', {})
    gyr = sensor_data.get('gyro', {})

    cursor.execute("""
        INSERT INTO telemetry_logs (
            timestamp, node_id, seq_num, frame_len, crc_status,
            tof_distance_mm, accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        node_id,
        seq_num,
        frame_len,
        crc_status,
        tof_val,
        acc.get('x'), acc.get('y'), acc.get('z'),
        gyr.get('x'), gyr.get('y'), gyr.get('z')
    ))
    conn.commit()
    conn.close()

init_db()

try:
    ser = serial.Serial(COM_PORT, baudrate=BAUD_RATE, timeout=1.0)
    print(f"Base station logging active on {COM_PORT}. Database: {DB_FILE}\n")

    rx_buffer = bytearray()

    while True:
        if ser.in_waiting > 0:
            rx_buffer.extend(ser.read(ser.in_waiting))

            while len(rx_buffer) >= 6:
                sync_idx = rx_buffer.find(b'\xAA\x55')
                if sync_idx == -1:
                    rx_buffer.clear()
                    break
                if sync_idx > 0:
                    rx_buffer = rx_buffer[sync_idx:]
                if len(rx_buffer) < 6:
                    break

                payload_len = rx_buffer[5]
                total_frame_len = 6 + payload_len + 2
                if len(rx_buffer) < total_frame_len:
                    break

                frame = bytes(rx_buffer[:total_frame_len])
                rx_buffer = rx_buffer[total_frame_len:]

                raw_data = frame[:-2]
                received_crc = struct.unpack(">H", frame[-2:])[0]
                computed_crc = calculate_crc16(raw_data)

                if received_crc == computed_crc:
                    sync, node_id, seq_num, p_len = struct.unpack(">HBHB", raw_data[:6])
                    payload = raw_data[6:]
                    sensor_data = parse_tlv_payload(payload)

                    log_to_database(node_id, seq_num, total_frame_len, "VALID", sensor_data)

                    print(f"[VALID FRAME] Node: {node_id} | Seq: #{seq_num:05d} | Len: {total_frame_len:02d}B | CRC: OK")
                    if 'tof_mm' in sensor_data:
                        tof_val = sensor_data['tof_mm']
                        print(f"  \u251c\u2500 ToF Distance : {tof_val:4d} mm ({tof_val / 10.0:5.1f} cm)")
                    if 'accel' in sensor_data:
                        acc = sensor_data['accel']
                        gyr = sensor_data['gyro']
                        print(f"  \u251c\u2500 Accelerometer: X = {acc['x']:+5.2f} m/s\u00b2 | Y = {acc['y']:+5.2f} m/s\u00b2 | Z = {acc['z']:+5.2f} m/s\u00b2")
                        print(f"  \u2514\u2500 Gyroscope    : X = {gyr['x']:+6.3f} rad/s | Y = {gyr['y']:+6.3f} rad/s | Z = {gyr['z']:+6.3f} rad/s")
                else:
                    print(f"[CRC MISMATCH] Dropped corrupted packet! Expected: 0x{computed_crc:04X}, Got: 0x{received_crc:04X}")

except KeyboardInterrupt:
    print("\nBase station stopped.")
finally:
    ser.close()
