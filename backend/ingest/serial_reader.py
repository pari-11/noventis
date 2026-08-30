"""
backend/ingest/serial_reader.py -- owns the pyserial port.

This is the ONLY module in the backend allowed to import pyserial or touch the
serial device. Everything downstream consumes the event bus.

The pyserial read loop is blocking, so it must NOT run on the FastAPI event loop
(constraint #3). Run it in a dedicated background thread (or asyncio.to_thread)
and hand decoded frames back to the loop to publish onto event_bus.bus.

Responsibilities:
  1. Open the serial port (config: NOVENTIS_SERIAL_PORT, NOVENTIS_SERIAL_BAUD).
  2. Byte-stream reassembly (see docs/protocol-spec.md "Stream framing";
     backend/legacy/base_rx.py has a working reference loop):
       - scan for SYNC (0xAA 0x55)
       - read the 6-byte header, take PAYLOAD_LEN (byte at offset 5)
       - read PAYLOAD_LEN + 2 more bytes (payload + CRC)
       - hand the candidate frame to protocol.parse_frame
  3. Resync on ProtocolError / CRC failure: advance one byte past the bad SYNC.
  4. Publish an event for EVERY candidate frame -- CRC pass AND fail -- because
     raw_frames is a forensic log.
  5. Reconnect with backoff if the port disappears (USB unplug).

Public API (driven by main.py lifespan):
    async def start() -> None      # spins up the reader task/thread
    async def stop()  -> None      # signals stop, joins, closes the port

TODO:
  - [ ] Choose: threading.Thread + loop.call_soon_threadsafe(bus.publish, ...)
        vs asyncio.to_thread per read. Thread is the safer default for pyserial.
  - [ ] Implement _frame_scanner(buffer) generator that yields complete candidate
        frames and keeps the trailing partial bytes.
  - [ ] Backoff/reconnect loop around serial.Serial(...).
  - [ ] Build the FrameEvent dict (see event_bus.FrameEvent) and publish it.
  - [ ] Clean shutdown: stop Event + thread.join(timeout) + ser.close().
  - [ ] Structured logging: bytes in, frames ok, frames bad, resyncs.
"""

from __future__ import annotations

import os

# Defaults match the original base station (base_rx.py): COM4 @ 9600 baud.
SERIAL_PORT = os.getenv("NOVENTIS_SERIAL_PORT", "COM4")
SERIAL_BAUD = int(os.getenv("NOVENTIS_SERIAL_BAUD", "9600"))


async def start() -> None:
    raise NotImplementedError("serial_reader.start(): launch the background read loop")


async def stop() -> None:
    raise NotImplementedError("serial_reader.stop(): signal + join + close the port")
