"""
backend/control/shutdown.py -- remote node shutdown over LoRa.

Sends the authenticated shutdown command (protocol-spec section 8) to a node and
reports what happened. An independent event-bus subscriber (``"control"``,
constraint #4); it never touches the serial port -- bytes go out through
``SerialReader.send()`` and the reader thread does the write (constraint #3).

One attempt, repeated until ``ATTEMPT_WINDOW_S`` runs out::

    send command -> stay quiet for LISTEN_S so the gateway can hear the reply

LoRa is half duplex: while this side transmits it cannot receive, and the node
cannot receive while it transmits its own telemetry. Hence stop-and-wait with a
quiet window instead of a fast blind loop.

Outcomes (``ShutdownResult.outcome``):

  acked         the node confirmed; it is powering off now.
  silent        no ACK, and no telemetry from the node for ``SILENT_AFTER_S``.
                Probably off (the ACK may simply have been lost) -- but it could
                equally already have been off / out of range. Unconfirmed.
  not_received  no ACK and the node is still transmitting telemetry: the command
                did not get through.

The counter is wall-clock seconds (strictly increasing within a run). The node
accepts only a counter greater than the last it saw, so replaying a captured
command does nothing; being time-based means no state file here to lose. If this
machine's clock is ever set far into the future, commands after it are refused
until the clock catches up -- see docs/architecture.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import NamedTuple

from ..ingest import protocol

log = logging.getLogger(__name__)

# Total time spent trying before giving up.
ATTEMPT_WINDOW_S = float(os.getenv("NOVENTIS_SHUTDOWN_WINDOW_S", "10"))
# Quiet time after each send, waiting for the ACK (the node ACKs 3x, ~0.3 s apart).
LISTEN_S = float(os.getenv("NOVENTIS_SHUTDOWN_LISTEN_S", "1.0"))
# "Silent" = no telemetry from the node for at least this long when we give up.
SILENT_AFTER_S = float(os.getenv("NOVENTIS_SHUTDOWN_SILENT_AFTER_S", "3.0"))

MIN_SECRET_LEN = 8


class ControlError(Exception):
    """A request that cannot be attempted. ``status`` is the HTTP status to use."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ShutdownResult(NamedTuple):
    outcome: str      # acked | silent | not_received
    attempts: int
    message: str


_MESSAGES = {
    "acked": "Node confirmed - it is shutting down.",
    "silent": ("No confirmation, but the node has stopped transmitting - it is probably "
               "off (or was already off / out of range)."),
    "not_received": "No response - the node is still transmitting, so it did not get the command.",
}


class ShutdownController:
    def __init__(self, bus, get_reader):
        self._bus = bus
        self._get_reader = get_reader          # callable -> SerialReader (resolved late)
        self._task: asyncio.Task | None = None
        self._waiters: dict[tuple[int, int], asyncio.Event] = {}
        self._last_telemetry: dict[int, float] = {}   # node_id -> time.monotonic()
        self._busy: set[int] = set()
        self._last_counter = 0
        self._requests = 0
        self._sent = 0
        self._acks = 0
        self._last_outcome: str | None = None

    # -- lifecycle (event-bus subscriber) -------------------------------- #
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="control")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        log.info("control subscribed to event bus")
        async for event in self._bus.subscribe("control"):
            try:
                self._observe(event)
            except Exception:  # pragma: no cover - a bad event must not kill the loop
                log.exception("control: failed to process event")

    def _observe(self, event: dict) -> None:
        node_id = event.get("node_id")
        if node_id is None or not event.get("crc_ok"):
            return
        ack = event["values"].get("cmd_ack")
        if ack is not None:
            waiter = self._waiters.get((node_id, ack["counter"]))
            if waiter is not None:
                self._acks += 1
                waiter.set()
        elif not event.get("control"):
            self._last_telemetry[node_id] = time.monotonic()

    # -- the operation ---------------------------------------------------- #
    def _secret(self) -> bytes:
        secret = os.getenv("NOVENTIS_CMD_SECRET", "")
        if len(secret) < MIN_SECRET_LEN:
            raise ControlError(
                409, f"remote shutdown is not configured: set NOVENTIS_CMD_SECRET "
                     f"(at least {MIN_SECRET_LEN} characters) on the backend"
            )
        return secret.encode("utf-8")

    def _next_counter(self) -> int:
        self._last_counter = max(self._last_counter + 1, int(time.time())) & 0xFFFFFFFF
        return self._last_counter

    async def shutdown(self, node_id: int) -> ShutdownResult:
        secret = self._secret()
        reader = self._get_reader()
        if reader is None or not reader.connected:
            raise ControlError(409, "the LoRa radio (gateway) is not connected")
        if node_id in self._busy:
            raise ControlError(409, f"a shutdown for node {node_id} is already in progress")

        self._busy.add(node_id)
        self._requests += 1
        counter = self._next_counter()
        frame = protocol.build_command_frame(secret, node_id, protocol.ACTION_SHUTDOWN, counter)
        waiter = asyncio.Event()
        self._waiters[(node_id, counter)] = waiter
        attempts = 0
        log.warning("shutdown requested for node %s (counter %d)", node_id, counter)
        try:
            deadline = time.monotonic() + ATTEMPT_WINDOW_S
            while True:
                if reader.send(frame):
                    attempts += 1
                    self._sent += 1
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=LISTEN_S)
                    outcome = "acked"
                    break
                except asyncio.TimeoutError:
                    pass
                if time.monotonic() >= deadline:
                    last = self._last_telemetry.get(node_id)
                    quiet = last is None or (time.monotonic() - last) >= SILENT_AFTER_S
                    outcome = "silent" if quiet else "not_received"
                    break
        finally:
            self._waiters.pop((node_id, counter), None)
            self._busy.discard(node_id)

        self._last_outcome = outcome
        log.warning("shutdown node %s: %s after %d attempt(s)", node_id, outcome, attempts)
        return ShutdownResult(outcome, attempts, _MESSAGES[outcome])

    # -- introspection (GET /health) ------------------------------------ #
    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "configured": len(os.getenv("NOVENTIS_CMD_SECRET", "")) >= MIN_SECRET_LEN,
            "requests": self._requests,
            "commands_sent": self._sent,
            "acks_received": self._acks,
            "last_outcome": self._last_outcome,
        }
