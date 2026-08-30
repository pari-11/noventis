# Noventis

Multi-node LoRa telemetry. Battery edge nodes read Time-of-Flight + IMU sensors
and transmit TLV-encoded frames over LoRa. A gateway radio delivers frames to a
host over USB serial; a FastAPI backend validates, stores, and streams them; a
React dashboard shows live and historical data.

> **Status: scaffold.** `protocol.py` (edge + backend), the DB models/session,
> and the event bus are implemented. Everything else is a documented stub with
> TODOs. See [CLAUDE.md](CLAUDE.md) for the constraints and what's done.

## Layout

```
edge/       per-node script + shared protocol codec + sensor wrappers
backend/    FastAPI: serial ingest -> event bus -> {DB writer, WebSocket manager} + REST
frontend/   React + Vite + TypeScript dashboard
docs/       architecture + wire-protocol spec (the source of truth) + handover/
```

## Key design points

- **One wire spec, two codecs.** [docs/protocol-spec.md](docs/protocol-spec.md)
  is authoritative; `edge/protocol.py` and `backend/ingest/protocol.py` are a
  byte-for-byte mirror of each other. Keep all three in sync in one commit.
- **Serial never blocks the event loop.** The pyserial read loop runs in a
  background thread and publishes to an in-process event bus.
- **Decoupled consumers.** The DB writer and the WebSocket manager each subscribe
  to the event bus independently; neither touches the serial port.
- **Forensic + clean split.** `raw_frames` logs every packet (CRC pass or fail);
  `readings` holds only CRC-valid decoded values, with a unique
  `(node_id, seq_num)` for idempotent re-ingestion.

## Quick start

### Protocol self-test (no deps)

```
python edge/protocol.py
```

### Backend (dev)

```
cd backend
python -m venv .venv && . .venv/Scripts/activate   # Windows; use bin/activate on POSIX
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Config via environment: `NOVENTIS_SERIAL_PORT` (default `COM3`),
`NOVENTIS_SERIAL_BAUD` (`115200`), `NOVENTIS_DB_URL`
(`sqlite+aiosqlite:///./noventis.db`).

### Frontend (dev)

```
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies API + WS to :8000
```

## Handover

Long-form documentation goes in `docs/handover/Noventis_Documentation.pdf`
(added out of band, not tracked here).
