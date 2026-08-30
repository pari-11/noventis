# Noventis

Multi-node LoRa telemetry. Battery edge nodes read Time-of-Flight + IMU sensors
and transmit TLV-encoded frames over LoRa. A CP2102 USB base station feeds frames
to a FastAPI backend that validates, stores, and streams them; a React dashboard
shows live and historical data.

> **Status: implemented end to end.** Serial ingest → event bus → DB writer +
> WebSocket broadcaster, REST API, and dashboard are all built and integration-
> tested. Only `edge/sensors/{tof,imu}.py` remain stubs (the production
> `node_tx.py` talks to the sensor drivers directly). See [CLAUDE.md](CLAUDE.md).

## Layout

```
edge/       node_tx.py (production TX) + shared protocol codec + sensor wrappers
backend/    FastAPI: serial ingest -> event bus -> {DB writer, WebSocket manager} + REST
frontend/   React + Vite + TypeScript dashboard
docs/       architecture + wire-protocol spec (source of truth) + handover/
```

## Key design points

- **One wire spec, two codecs.** [docs/protocol-spec.md](docs/protocol-spec.md)
  is authoritative; `edge/protocol.py` and `backend/ingest/protocol.py` are a
  byte-for-byte mirror. Keep all three in sync, in one commit.
- **Serial never blocks the event loop.** `serial_reader.py` runs the pyserial
  loop in a background thread, auto-detects the CP2102 adapter by USB VID:PID
  `10C4:EA60`, and publishes decoded frames onto an in-process event bus.
- **Decoupled consumers.** `db/writer.py` and `ws/manager.py` each `subscribe()`
  to the bus independently; neither imports pyserial.
- **Forensic + clean split.** `raw_frames` logs every candidate frame (CRC pass
  or fail); `readings` holds only CRC-valid decoded values, with a unique
  `(node_id, seq_num)` so re-ingestion is idempotent.

## Run it

### 1. Backend  (from the repo root, not `backend/`)

```
python -m venv .venv
. .venv/Scripts/activate            # Windows; use .venv/bin/activate on POSIX
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload --port 8000
```

`http://localhost:8000/health` shows whether a CP2102 LoRa adapter is connected
and how many nodes have been seen. Interactive API docs at `/docs`.

Environment overrides (all optional):

| var | default | meaning |
|-----|---------|---------|
| `NOVENTIS_SERIAL_PORT` | *(autodetect CP2102)* | force a specific device, bypassing VID:PID detection |
| `NOVENTIS_NODE_PORT_MAP` | *(none)* | `"<serial-or-device>=<label>,..."` — required only when several CP2102 adapters are plugged in |
| `NOVENTIS_SERIAL_BAUD` | `9600` | E22 UART rate (8N1) |
| `NOVENTIS_DB_URL` | `sqlite+aiosqlite:///./noventis.db` | database |
| `NOVENTIS_STALE_AFTER_S` | `10` | a node with no frame within this many seconds is `stale` |
| `NOVENTIS_CORS_ORIGINS` | `http://localhost:5173` | comma-separated allowed origins |

### 2. Frontend

```
cd frontend
npm install
npm run dev            # http://localhost:5173 — proxies /health /nodes /readings /raw-frames /live to :8000
```

### Protocol self-test (no dependencies)

```
python edge/protocol.py
```

## What you should see when a remote node powers on

**Base station console** (uvicorn):

```
INFO  backend.ingest.serial_reader: LoRa base station connected on COM4
```
then, as frames arrive, `db writer` / `ws manager` stay quiet on the happy path;
CRC failures log `event-bus subscriber lagging` only if a consumer stalls.
`GET /health` flips to `"lora": { "connected": true, "port": "COM4", "frames_ok": <rising> }`
and `"nodes_seen": 1`.

**Browser** (`http://localhost:5173`):

- header pill turns green: **LoRa connected · COM4**, plus `1 node(s) seen`
- **Node 1** appears in the selector with a green (live) dot
- **Live** panel: `ws` badge `open`; the ToF / |acceleration| / |angular rate|
  sparklines start drawing, ~2 points/second
- pick **Node 1** → **History** panel loads the last 100 rows from `/readings`

If no adapter is plugged in, `/health` reports
`"connected": false, "last_error": "no CP2102 adapter detected …"` and the reader
rescans every 3 s — no crash.

## Handover

Long-form hardware/wiring documentation: `docs/handover/Noventis Documentation.pdf`
(added out of band, git-ignored).
