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
- `ingest/frame_buffer.py` -- **event-bus subscriber.** Bounded in-memory ring
  (`RAW_FRAME_BUFFER_SIZE`, default 5000 ≈ 40 min at 2 Hz) of the most recent
  candidate frames, CRC pass and fail. Process-lifetime only, never persisted;
  window onto it via `GET /debug/frames`.
- `db/writer.py` -- **event-bus subscriber.** Writes `raw_frames` only for
  **CRC-failed** frames; `readings` + `nodes.last_seen` when `crc_ok`.
- `ws/manager.py` -- **event-bus subscriber.** Fans CRC-valid readings out to
  `/live` WebSocket clients, filtered by `node_id`.
- `db/retention.py` -- periodic task (hourly). Rolls `readings` older than
  `READINGS_FULL_RES_HOURS` (default **168** = 7 days) into per-node
  `ROLLUP_INTERVAL_MINUTES` buckets in `readings_rollup` (avg + min + max),
  deletes the originals, and runs `PRAGMA incremental_vacuum`. Single tier. Its
  one-time `_prepare` step only converts the vacuum mode -- it never deletes.
- `db/models.py`, `db/session.py` -- SQLAlchemy 2.0 async ORM over SQLite (WAL).
- `api/nodes.py`, `api/readings.py`, `api/raw_frames.py`, `api/debug.py` -- REST routers.
- `scripts/backup_db.py` -- consistent snapshot (`VACUUM INTO`) safe to run with
  the backend live. `scripts/migrate_purge_legacy_frames.py` -- deliberate,
  prompted, operator-run purge of pre-model-change CRC-valid `raw_frames` rows
  (never done automatically).

### frontend/  (React + Vite + TypeScript)
- `App.tsx` owns the selected `node_id`.
- `NodeSelector` (from `GET /nodes`), `LiveChart` (from `useWebSocket` -> `/live`),
  `HistoryPanel` (from `GET /readings`).

## Data flow

```
serial bytes  (0xAA55-framed; see protocol-spec.md)
  -> serial_reader: resync on SYNC, slice candidate frame, parse_frame()
  -> event_bus.publish(FrameEvent{ raw, crc_ok, node_id, seq_num, values, received_at })
       |-> frame_buffer: append to the in-memory ring (every candidate frame)
       |-> db.writer   : if NOT crc_ok -> INSERT raw_frames;
       |                 if crc_ok -> UPSERT node, INSERT reading (idempotent)
       `-> ws.manager  : if crc_ok -> JSON to matching /live clients
```

Separately, `db.retention` runs hourly off the request path: it aggregates
`readings` rows older than `READINGS_FULL_RES_HOURS` into `readings_rollup`
buckets and deletes them. `GET /readings` merges the two tiers on read.

`values` holds the decoded keys `tof_mm`, `accel_mss`, `gyro_rads`
(see protocol-spec.md §3); `db.writer` flattens the vectors into the
`accel_{x,y,z}` / `gyro_{x,y,z}` columns. `ingest/serial_reader.py` also adds a
derived `tof_out_of_range` bool (raw `tof_mm` above `TOF_MAX_VALID_MM`, the
VL53L0X no-target sentinel) — carried on the bus and stored in
`readings.tof_out_of_range`. The wire format and both `protocol.py` modules are
unchanged.

`frame_buffer`, `db.writer` and `ws.manager` subscribe **independently**. None
imports pyserial or touches the port. If one is slow or crashes, the others are
unaffected.

## Data model

| Table        | Purpose                        | Key columns                                                   | Constraints |
|--------------|--------------------------------|--------------------------------------------------------------|-------------|
| `nodes`      | known nodes                    | `node_id` PK, `last_seen`, `name` (nullable)                  | `status` computed on read, never stored; `name` falls back to `"Node {id}"` on read when NULL |
| `raw_frames` | forensic log of **CRC-failed** frames only | `id` PK, `node_id` (nullable), `received_at`, `crc_ok`, `raw` | index on `received_at`, `node_id`; grows negligibly (~12/24k), left unpruned |
| `readings`   | CRC-valid decoded values, full-res, last `READINGS_FULL_RES_HOURS` | `id` PK, `node_id`, `seq_num`, `timestamp`, `tof_mm`, `tof_out_of_range` (derived), `accel_{x,y,z}` (m/s^2), `gyro_{x,y,z}` (rad/s) | index `(node_id, timestamp)`; **unique `(node_id, seq_num)`** |
| `readings_rollup` | per-node time-bucket aggregates of `readings` older than that window | `id` PK, `node_id`, `bucket_start`, `sample_count`, `tof_mm_{avg,min,max}`, `accel_{x,y,z}_{avg,min,max}`, `gyro_{x,y,z}_{avg,min,max}` | index + **unique `(node_id, bucket_start)`** |

CRC-valid frames are **not** persisted -- the most recent ~5000 live in
`ingest/frame_buffer.py`'s in-memory ring (`GET /debug/frames`) and are gone on
restart. Only CRC-*failed* frames reach the `raw_frames` table.

`readings_rollup` stores min/max **alongside** avg so a one-minute average never
erases a short accel/gyro spike. Single tier -- no coarser rollup on top.
`db/retention.py` fills it hourly (bucket-aligned cutoff so no bucket is ever
split), processing any backlog in `BACKLOG_BATCH_HOURS`-wide transactions.
Reclaims space with `PRAGMA auto_vacuum=INCREMENTAL` + periodic
`incremental_vacuum` (a one-time full `VACUUM` at first run converts an existing
DB to incremental mode).

### Full-resolution window & storage

`READINGS_FULL_RES_HOURS` (env `NOVENTIS_READINGS_FULL_RES_HOURS`, **default 168 =
7 days**) is the production tuning knob: how far back full-resolution ~0.5 s
(2 Hz) data stays in `readings` before it is collapsed to 1-minute
avg/min/max rows and the originals are **permanently deleted**. Cost is linear:

| | rows | on disk (measured ~156 B/row incl. both indexes) |
|---|--:|--:|
| `readings`, 1 node, 7 days @ 2 Hz | ~1.21 M | **~188 MB** |
| `readings`, 3 nodes, 7 days       | ~3.63 M | ~565 MB |
| `readings`, 5 nodes, 7 days       | ~6.05 M | ~940 MB |
| `readings_rollup`, per node       | 10,080 / week (~525 k/yr) | ~2.5 MB/week (~130 MB/yr) |

The full-res portion is bounded (retention holds the 7-day window); a single-node
deployment stays well under 200 MB and even five nodes under 1 GB -- comfortable
for SQLite. Only `readings_rollup` grows unbounded, and slowly; a second coarser
tier (deferred) would cap that if it ever matters.

**Backups.** The dataset is one SQLite file. `scripts/backup_db.py` takes a
consistent `VACUUM INTO` snapshot safely while the backend runs (a raw copy of a
live WAL DB can be torn). Run it at least daily (cron / Task Scheduler).
Fine-grained readings older than `READINGS_FULL_RES_HOURS` are gone once rolled
up -- if the raw 2 Hz data matters, a backup must land inside that window.

The unique `(node_id, seq_num)` constraint makes re-ingestion (replaying a serial
capture, restarting the reader) idempotent *within a session* -- duplicate
readings are dropped with `ON CONFLICT DO NOTHING`.

A **node reboot** resets its `seq_num` to ~0, which would otherwise collide with
the prior session and freeze that node's readings. `db/writer.py` watches each
node's `seq_num`: a large backward jump (not the `0xFFFF -> 0x0000` wrap) after a
gap of silence is a restart, and it clears that node's `readings` so the new
session ingests. `writer.status().sessions_reset` counts how often it has fired.
(It does not touch `readings_rollup`; historical buckets survive a reset.)

Node `status` is derived: `online` if `now - last_seen <= NODE_TIMEOUT`
(env-configured, default ~30 s), else `offline`.

Persistence: SQLAlchemy async ORM, SQLite in **WAL mode** (concurrent
writer + API readers). No Alembic yet -- `Base.metadata.create_all` at startup.

## API contract

| Method | Path                        | Notes                                                   |
|--------|-----------------------------|--------------------------------------------------------|
| GET    | `/nodes`                    | all nodes + computed `status`                          |
| GET    | `/readings?node_id=<id>`    | readings newest first, paginated; when `since` reaches past `READINGS_FULL_RES_HOURS` the older portion is served from `readings_rollup`, merged transparently (bucketed rows carry `rollup: true` + `sample_count`) |
| GET    | `/raw-frames?node_id=<id>`  | `raw_frames` table -- CRC-failed frames only now; `node_id` optional (NULL for bad header) |
| GET    | `/debug/frames`             | window onto the in-memory frame ring buffer (recent frames, CRC pass + fail); `limit`, `crc_ok`, `node_id` filters; empty after a restart |
| PATCH  | `/nodes/{node_id}`          | set display name; body `{"name": "<1..64 chars>"}`; returns the updated node (404 unknown node, 422 blank name) |
| POST   | `/rescan`                   | force serial CP2102 auto-detect to re-run; waits a bounded window and returns `{ok, connected, port, last_error}` |
| WS     | `/live?node_id=<id>`        | stream of CRC-valid readings; omit `node_id` for all   |

`GET /health` additionally reports `frame_buffer` (ring `size`/`capacity`/
`received_total`) and `retention` (`passes`, `buckets_written`,
`readings_rolled_up`, `last_pass_at`, `auto_vacuum_mode`, …) so both are
verifiable at a glance.

`POST /rescan` sets a flag on the `SerialReader` background thread
(`request_rescan()`), which drops any open port and re-runs `select_port` on its
next loop pass (within a read timeout). The handler then polls `reader.status()`
for up to `NOVENTIS_RESCAN_WAIT_S` (default 4 s) so the response reflects the
real reconnect outcome, not a timer. Still the only pyserial user is
`ingest/serial_reader.py`.

## Wire protocol

See [protocol-spec.md](protocol-spec.md) -- the source of truth for both
`protocol.py` modules.

## Deferred

- Alembic migrations (`init_db()` runs `create_all` on startup, plus a tiny
  idempotent `_retrofit_columns` shim for ADD-COLUMN-only changes `create_all`
  can't apply to an existing table).
- Auth on the API / WebSocket.
- Multi-process or multi-host fan-out (event bus is in-process only).
- A second, coarser `readings_rollup` tier (deliberately not built -- one tier
  stays small enough and keeps the history-API merge simple).
- Persisting the recent-frames ring buffer across restarts (by design it is
  in-memory only).
- `edge/sensors/{tof,imu}.py` extraction (`node_tx.py` uses the drivers inline).
- Multiple simultaneous base stations (`serial_reader` reads one CP2102; a
  second adapter requires `NODE_PORT_MAP` and today still yields a single reader).
- Richer charting (the dashboard uses hand-rolled SVG sparklines).
- Automated/off-site backup rotation (`scripts/backup_db.py` is manual / cron;
  it does not prune or ship old snapshots).
- A server-side "all history" downsample on `GET /readings` (today the endpoint
  merges `readings` + `readings_rollup` but is `limit`-bounded, max 2000 rows;
  it does not decimate a multi-week span into a fixed point budget).
