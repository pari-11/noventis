# Noventis Architecture

Multi-node LoRa telemetry. Battery-powered edge nodes read ToF + IMU sensors and
transmit TLV-encoded frames over LoRa. A gateway radio delivers those frames to a
host over serial (USB). The backend validates, stores, and streams them; a web
dashboard shows live and historical data.

```
  [edge node] --LoRa--> [gateway radio] --USB serial--> [backend host] --HTTP/WS--> [browser]
   ToF + IMU              (transparent)                   FastAPI                     React/Vite
```

## Components

### edge/  (per-node firmware / script)
- `node_tx.py` -- **production script** (user-owned). Reads sensors, builds a
  frame, transmits over LoRa on a duty cycle. Not rewritten by this project.
- `sensors/tof.py`, `sensors/imu.py` -- thin board-agnostic driver wrappers.
- `protocol.py` -- shared TLV + CRC-16 codec. Imported by `node_tx.py`.

### backend/  (FastAPI, async)
- `main.py` -- app + lifespan that starts every moving part in order.
- `ingest/serial_reader.py` -- **the only pyserial user.** Blocking read loop
  runs off the event loop (background thread / `asyncio.to_thread`). Reassembles
  the byte stream into candidate frames, calls `protocol.parse_frame`, and
  publishes a `FrameEvent` for **every** candidate (CRC pass and fail).
- `ingest/protocol.py` -- byte-for-byte mirror of `edge/protocol.py`.
- `ingest/event_bus.py` -- in-process asyncio pub/sub. Per-subscriber bounded
  queue; slow consumer drops its own oldest events, never blocks ingestion.
- `db/writer.py` -- **event-bus subscriber.** Writes `raw_frames` always;
  `readings` + `nodes.last_seen` when `crc_ok`.
- `ws/manager.py` -- **event-bus subscriber.** Fans CRC-valid readings out to
  `/live` WebSocket clients, filtered by `node_id`.
- `db/models.py`, `db/session.py` -- SQLAlchemy 2.0 async ORM over SQLite (WAL).
- `api/nodes.py`, `api/readings.py`, `api/raw_frames.py` -- REST routers.

### frontend/  (React + Vite + TypeScript)
- `App.tsx` owns the selected `node_id`.
- `NodeSelector` (from `GET /nodes`), `LiveChart` (from `useWebSocket` -> `/live`),
  `HistoryPanel` (from `GET /readings`).

## Data flow

```
serial bytes  (0xAA55-framed; see protocol-spec.md)
  -> serial_reader: resync on SYNC, slice candidate frame, parse_frame()
  -> event_bus.publish(FrameEvent{ raw, crc_ok, node_id, seq_num, values, received_at })
       |-> db.writer   : INSERT raw_frames; if crc_ok -> UPSERT node, INSERT reading (idempotent)
       `-> ws.manager  : if crc_ok -> JSON to matching /live clients
```

`values` holds the decoded keys `tof_mm`, `accel_mss`, `gyro_rads`
(see protocol-spec.md §3); `db.writer` flattens the vectors into the
`accel_{x,y,z}` / `gyro_{x,y,z}` columns. `ingest/serial_reader.py` also adds a
derived `tof_out_of_range` bool (raw `tof_mm` above `TOF_MAX_VALID_MM`, the
VL53L0X no-target sentinel) — carried on the bus and stored in
`readings.tof_out_of_range`. The wire format and both `protocol.py` modules are
unchanged.

`db.writer` and `ws.manager` subscribe **independently**. Neither imports
pyserial or touches the port. If one is slow or crashes, the other is unaffected.

## Data model

| Table        | Purpose                        | Key columns                                                   | Constraints |
|--------------|--------------------------------|--------------------------------------------------------------|-------------|
| `nodes`      | known nodes                    | `node_id` PK, `last_seen`                                     | `status` computed on read, never stored |
| `raw_frames` | forensic log of every packet   | `id` PK, `node_id` (nullable), `received_at`, `crc_ok`, `raw` | index on `received_at`, `node_id` |
| `readings`   | CRC-valid decoded values only  | `id` PK, `node_id`, `seq_num`, `timestamp`, `tof_mm`, `tof_out_of_range` (derived), `accel_{x,y,z}` (m/s^2), `gyro_{x,y,z}` (rad/s) | index `(node_id, timestamp)`; **unique `(node_id, seq_num)`** |

The unique `(node_id, seq_num)` constraint makes re-ingestion (replaying a serial
capture, restarting the reader) idempotent *within a session* -- duplicate
readings are dropped with `ON CONFLICT DO NOTHING`.

A **node reboot** resets its `seq_num` to ~0, which would otherwise collide with
the prior session and freeze that node's readings. `db/writer.py` watches each
node's `seq_num`: a large backward jump (not the `0xFFFF -> 0x0000` wrap) after a
gap of silence is a restart, and it clears that node's `readings` so the new
session ingests. `raw_frames` keeps everything; `writer.status().sessions_reset`
counts how often it has fired.

Node `status` is derived: `online` if `now - last_seen <= NODE_TIMEOUT`
(env-configured, default ~30 s), else `offline`.

Persistence: SQLAlchemy async ORM, SQLite in **WAL mode** (concurrent
writer + API readers). No Alembic yet -- `Base.metadata.create_all` at startup.

## API contract

| Method | Path                        | Notes                                                   |
|--------|-----------------------------|--------------------------------------------------------|
| GET    | `/nodes`                    | all nodes + computed `status`                          |
| GET    | `/readings?node_id=<id>`    | CRC-valid readings, newest first, paginated            |
| GET    | `/raw-frames?node_id=<id>`  | forensic log; `node_id` optional (NULL for bad header) |
| WS     | `/live?node_id=<id>`        | stream of CRC-valid readings; omit `node_id` for all   |

## Wire protocol

See [protocol-spec.md](protocol-spec.md) -- the source of truth for both
`protocol.py` modules.

## Deferred

- Alembic migrations (`init_db()` runs `create_all` on startup, plus a tiny
  idempotent `_retrofit_columns` shim for ADD-COLUMN-only changes `create_all`
  can't apply to an existing table).
- Auth on the API / WebSocket.
- Multi-process or multi-host fan-out (event bus is in-process only).
- Downsampling / retention for `raw_frames`.
- `edge/sensors/{tof,imu}.py` extraction (`node_tx.py` uses the drivers inline).
- Multiple simultaneous base stations (`serial_reader` reads one CP2102; a
  second adapter requires `NODE_PORT_MAP` and today still yields a single reader).
- Richer charting (the dashboard uses hand-rolled SVG sparklines).
