"""
backend/ingest/serial_reader.py -- owns the LoRa base-station serial port.

This is the ONLY backend module that imports pyserial or touches a serial
device. Everything downstream consumes the event bus.

Design (project constraint #3):
  * The pyserial read loop is blocking, so it runs in a dedicated background
    thread -- never on the FastAPI event loop.
  * The port is NOT hardcoded. On startup (and whenever the port disappears) we
    scan ``serial.tools.list_ports.comports()`` and pick the CP2102 USB-to-UART
    adapter by its USB VID:PID ``10C4:EA60`` (per the hand-over guide: the base
    station is an Ebyte E22 wired to a CP2102).
      - 0 adapters  -> log "plug it in and restart", keep rescanning every few
                       seconds (no crash).
      - 1 adapter   -> use it.
      - >1 adapters -> log all of them and require NODE_PORT_MAP (config / env
                       ``NOVENTIS_NODE_PORT_MAP``) to say which one, since we may
                       run several base stations later.
  * For every complete candidate frame pulled off the stream we call
    ``protocol.parse_frame`` and publish a dict to the event bus -- CRC-valid
    frames decoded, CRC-invalid frames tagged ``crc_ok=False`` (so db.writer can
    still log them to ``raw_frames``).

The reader does not import asyncio or the event bus. ``main.py`` injects a
``publish`` callable that is safe to call from this thread (see
``loop_safe_publisher`` below).

Published event shape (see also ingest/event_bus.py)::

    {
      "node_id":     int | None,   # None if the header could not be parsed
      "seq_num":     int | None,
      "crc_ok":      bool,
      "raw":         bytes,        # exact candidate-frame bytes
      "raw_hex":     str,          # raw.hex(), convenience for JSON / logs
      "values":      dict,         # decoded TLV keys; {} when crc_ok is False.
                                   #   When a tof_mm key is present it is carried
                                   #   verbatim and a derived bool
                                   #   "tof_out_of_range" is added (see
                                   #   TOF_MAX_VALID_MM below).
      "received_at": datetime,     # timezone-aware UTC, stamped on receipt
      "control":     bool,         # True for a CRC-valid control frame (shutdown
                                   #   ACK, protocol-spec section 8). NOT telemetry:
                                   #   db.writer / archive.writer / ws.manager skip
                                   #   it; frame_buffer and /live/raw still see it.
    }

Outbound: ``send(bytes)`` queues bytes for THIS thread to write to the port
(the port belongs to this thread alone; callers never write to it directly).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone

import serial
from serial.tools import list_ports

try:  # package import (normal) / script import (python backend/ingest/serial_reader.py)
    from .protocol import ProtocolError, extract_frames, parse_frame
except ImportError:  # pragma: no cover
    from protocol import ProtocolError, extract_frames, parse_frame

log = logging.getLogger(__name__)

# CP2102 USB-to-UART bridge (Silicon Labs) -- the base-station adapter.
CP2102_VID = 0x10C4
CP2102_PID = 0xEA60

DEFAULT_BAUD = int(os.getenv("NOVENTIS_SERIAL_BAUD", "9600"))  # E22 UART is 9600 8N1
RESCAN_INTERVAL_S = 3.0
READ_TIMEOUT_S = 0.2
READ_CHUNK = 4096

# VL53L0X out-of-range sentinel ------------------------------------------------ #
# The VL53L0X reports a large fixed value (~0x1FFE / 0x1FFF mm, i.e. ~8.19 m)
# when it has no valid target. Confirmed against the forensic log: genuine
# readings top out below 1 m while no-target frames land dead on 8190/8191 mm,
# with nothing in between. A ToF value above this threshold is a "no target"
# marker, not a distance, so we flag it instead of letting it distort the
# dashboard chart's autoscale.
#
# This lives here -- in the ingest fan-out, downstream of the codec -- and NOT
# in protocol.decode_tlv: edge/protocol.py and backend/ingest/protocol.py must
# stay byte-for-byte identical (constraint #2), and this is an interpretation of
# the value, not a wire-format change. The raw value is still carried verbatim,
# in raw_frames and in the reading's tof_mm column, alongside the derived flag.
TOF_MAX_VALID_MM = int(os.getenv("NOVENTIS_TOF_MAX_VALID_MM", "2000"))


class NoAdapterFound(Exception):
    """No CP2102 adapter is currently connected."""


class AmbiguousAdapters(Exception):
    """Several CP2102 adapters are connected and NODE_PORT_MAP did not resolve one."""


# --------------------------------------------------------------------------- #
# Port discovery
# --------------------------------------------------------------------------- #
def find_cp2102_ports():
    """Return the list_ports entries whose USB VID:PID is the CP2102."""
    return [p for p in list_ports.comports() if (p.vid, p.pid) == (CP2102_VID, CP2102_PID)]


def _describe(ports):
    return ", ".join(
        f"{p.device} (sn={p.serial_number or '?'}, {p.description or '?'})" for p in ports
    )


def select_port(node_port_map=None, override=None):
    """Decide which serial device to open.

    override        -- explicit device path from NOVENTIS_SERIAL_PORT; bypasses
                       autodetection entirely (escape hatch for odd setups).
    node_port_map   -- {serial_number_or_device: label}; only consulted when more
                       than one CP2102 is present.
    """
    if override:
        return override

    ports = find_cp2102_ports()
    if not ports:
        raise NoAdapterFound(
            "no CP2102 adapter detected (USB 10C4:EA60) -- plug in the LoRa base "
            "station and restart"
        )
    if len(ports) == 1:
        return ports[0].device

    listing = _describe(ports)
    if not node_port_map:
        raise AmbiguousAdapters(
            f"{len(ports)} CP2102 adapters connected [{listing}]; set NODE_PORT_MAP "
            "(env NOVENTIS_NODE_PORT_MAP='<serial-or-device>=<label>,...') to choose one"
        )
    chosen = [
        p.device
        for p in ports
        if p.device in node_port_map or (p.serial_number in node_port_map)
    ]
    if len(chosen) != 1:
        raise AmbiguousAdapters(
            f"NODE_PORT_MAP {node_port_map!r} did not resolve to exactly one of [{listing}]"
        )
    return chosen[0]


def node_port_map_from_env():
    """Parse NOVENTIS_NODE_PORT_MAP='SERIAL=label,COM7=label' -> dict (or {})."""
    raw = os.getenv("NOVENTIS_NODE_PORT_MAP", "").strip()
    mapping = {}
    for pair in filter(None, (chunk.strip() for chunk in raw.split(","))):
        key, _, label = pair.partition("=")
        if key.strip():
            mapping[key.strip()] = label.strip() or key.strip()
    return mapping


# --------------------------------------------------------------------------- #
# Reader
# --------------------------------------------------------------------------- #
class SerialReader:
    """Runs the blocking pyserial loop in a background thread and fans decoded
    frames out through an injected, thread-safe ``publish(event: dict)``."""

    def __init__(self, publish, *, node_port_map=None, baud=DEFAULT_BAUD, port_override=None):
        self._publish = publish
        self._node_port_map = dict(node_port_map or {})
        self._baud = baud
        self._port_override = port_override or os.getenv("NOVENTIS_SERIAL_PORT") or None

        self._stop = threading.Event()
        # set by request_rescan() to make the thread drop any open port and
        # re-run autodetection immediately instead of waiting out RESCAN_INTERVAL_S
        self._rescan = threading.Event()
        self._thread: threading.Thread | None = None
        self._port: str | None = None          # open device path, or None when disconnected
        self._bytes_read = 0
        self._frames_ok = 0
        self._frames_bad = 0
        self._last_error: str | None = None
        # bytes queued by send() for this thread to write; bounded so a stuck
        # port can't accumulate commands without limit
        self._outbox: queue.Queue = queue.Queue(maxsize=32)
        self._frames_sent = 0

    # -- lifecycle (call from the asyncio side) ----------------------------- #
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lora-serial-reader", daemon=True)
        self._thread.start()
        log.info("serial reader thread started")

    def stop(self, timeout=2.0):
        self._stop.set()
        self._rescan.set()  # break any interruptible wait promptly
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        log.info("serial reader thread stopped")

    def request_rescan(self):
        """Drop any open port and re-run CP2102 autodetection now.

        Safe to call from any thread (e.g. a FastAPI request handler). The
        background thread picks it up within a read timeout (~0.2 s); it does not
        block the caller.
        """
        log.info("serial reader: rescan requested")
        self._rescan.set()

    def send(self, data: bytes) -> bool:
        """Queue ``data`` to be written to the radio. Safe from any thread; never
        blocks. Returns False when no port is open or the outbox is full.

        The reader thread does the actual write within one read timeout (~0.2 s).
        """
        if not self.connected:
            return False
        try:
            self._outbox.put_nowait(bytes(data))
            return True
        except queue.Full:
            return False

    def _sleep(self, seconds: float):
        """Wait up to ``seconds``, returning early on stop or a rescan request."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop.wait(0.1) or self._rescan.is_set():
                return

    # -- status (for GET /health) ---------------------------------------- #
    @property
    def connected(self) -> bool:
        return self._port is not None

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "port": self._port,
            "baud": self._baud,
            "bytes_read": self._bytes_read,
            "frames_ok": self._frames_ok,
            "frames_bad": self._frames_bad,
            "frames_sent": self._frames_sent,
            "last_error": self._last_error,
        }

    # -- thread body ------------------------------------------------------- #
    def _run(self):
        while not self._stop.is_set():
            self._rescan.clear()  # consume any pending request; act on it now
            try:
                device = select_port(self._node_port_map, self._port_override)
            except (NoAdapterFound, AmbiguousAdapters) as exc:
                self._last_error = str(exc)
                log.warning("%s", exc)
                self._sleep(RESCAN_INTERVAL_S)
                continue

            try:
                self._read_from(device)
            except serial.SerialException as exc:
                self._last_error = f"{device}: {exc}"
                log.warning("serial port %s error: %s -- rescanning in %.0fs",
                            device, exc, RESCAN_INTERVAL_S)
            except Exception:  # never let the thread die silently
                log.exception("unexpected serial reader failure -- rescanning in %.0fs",
                              RESCAN_INTERVAL_S)
            finally:
                self._port = None
            self._sleep(RESCAN_INTERVAL_S)

    def _read_from(self, device):
        log.info("opening LoRa serial port %s @ %d baud (8N1)", device, self._baud)
        with serial.Serial(
            device,
            baudrate=self._baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=READ_TIMEOUT_S,
        ) as ser:
            ser.reset_input_buffer()
            while True:                     # drop commands queued for a previous port
                try:
                    self._outbox.get_nowait()
                except queue.Empty:
                    break
            self._port = device
            self._last_error = None
            log.info("LoRa base station connected on %s", device)

            buf = bytearray()
            idle_ticks = 0
            while not self._stop.is_set() and not self._rescan.is_set():
                self._flush_outbox(ser)
                waiting = ser.in_waiting
                chunk = ser.read(waiting or 1)  # returns within READ_TIMEOUT_S when idle
                if not chunk:
                    idle_ticks += 1
                    if idle_ticks % 25 == 0:  # ~5 s of total silence
                        log.warning(
                            "%s: no bytes received in ~5s (base station wired for Mode 0 / "
                            "node transmitting?) -- %d bytes seen so far",
                            device, self._bytes_read,
                        )
                    continue
                idle_ticks = 0
                self._bytes_read += len(chunk)
                log.debug("%s: +%d bytes (%d total): %s",
                          device, len(chunk), self._bytes_read, chunk.hex())
                buf.extend(chunk)
                for frame in extract_frames(buf):
                    self._handle_frame(frame)
        log.info("closed LoRa serial port %s", device)

    def _flush_outbox(self, ser):
        """Write anything queued by send(). Runs on the reader thread only."""
        while True:
            try:
                data = self._outbox.get_nowait()
            except queue.Empty:
                return
            ser.write(data)
            ser.flush()
            self._frames_sent += 1
            log.info("%s: sent %d bytes to radio: %s", self._port, len(data), data.hex())

    def _handle_frame(self, frame: bytes):
        received_at = datetime.now(timezone.utc)
        raw = bytes(frame)
        node_id = seq_num = None
        crc_ok = False
        values: dict = {}
        try:
            decoded = parse_frame(raw)
            node_id, seq_num, crc_ok, values = (
                decoded.node_id, decoded.seq_num, decoded.crc_ok, decoded.values,
            )
        except ProtocolError as exc:
            # extract_frames should not emit structurally broken slices, but if a
            # CRC-valid frame carries a malformed TLV, decode raises here.
            log.debug("frame rejected structurally: %s", exc)

        if crc_ok:
            self._frames_ok += 1
        else:
            self._frames_bad += 1

        # Annotate (not rewrite) the ToF reading: keep the raw value, add a flag
        # so downstream consumers can tell a real distance from the VL53L0X
        # no-target sentinel. See TOF_MAX_VALID_MM.
        if crc_ok and values.get("tof_mm") is not None:
            values["tof_out_of_range"] = values["tof_mm"] > TOF_MAX_VALID_MM

        self._publish({
            "node_id": node_id,
            "seq_num": seq_num,
            "crc_ok": crc_ok,
            "raw": raw,
            "raw_hex": raw.hex(),
            "values": values,
            "received_at": received_at,
            "control": bool(crc_ok and ("cmd" in values or "cmd_ack" in values)),
        })


# --------------------------------------------------------------------------- #
# Thread -> event-loop bridge (used by main.py to wire this to the event bus)
# --------------------------------------------------------------------------- #
def loop_safe_publisher(loop, target):
    """Wrap ``target(event)`` so the reader thread can call it safely.

    ``target`` is the event bus's synchronous ``publish`` (fan-out to subscriber
    queues). The returned callable schedules it on ``loop`` via
    ``call_soon_threadsafe`` and returns immediately.
    """
    def _publish(event):
        loop.call_soon_threadsafe(target, event)

    return _publish


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _all = list_ports.comports()
    print(f"{len(_all)} serial port(s):")
    for _p in _all:
        _tag = " <- CP2102" if (_p.vid, _p.pid) == (CP2102_VID, CP2102_PID) else ""
        _vid = f"{_p.vid:04X}" if _p.vid else "----"
        _pid = f"{_p.pid:04X}" if _p.pid else "----"
        print(f"  {_p.device:8}  {_vid}:{_pid}  sn={_p.serial_number or '?':16}  {_p.description or ''}{_tag}")
    try:
        print("selected:", select_port(node_port_map_from_env(), os.getenv("NOVENTIS_SERIAL_PORT")))
    except (NoAdapterFound, AmbiguousAdapters) as _exc:
        print("selected: <none> --", _exc)
