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
  `/live` WebSocket clients, filtered by `node_id`, through a per-connection
  bounded outbox + send task -- one slow client drops only its own oldest
  messages (told via a `{"type":"gap"}` control message) and can never block
  the broadcast to anyone else.
- `archive/writer.py`, `archive/store.py` -- **event-bus subscriber** (the
  fourth, alongside the three above). System of record: every CRC-valid
  reading, full resolution, forever, in `archive/` -- see "Archive tier" below.
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
  (never done automatically). `scripts/verify_archive.py` -- reconciles
  `archive/` against `readings` and verifies every sealed manifest.
  `scripts/check_protocol_sync.py` / `sync_protocol.py` -- enforce and repair
  the two `protocol.py` modules staying byte-identical; wired into
  `.githooks/pre-commit`.

### frontend/  (React + Vite + TypeScript)
- `App.tsx` owns the selected `node_id`.
- `NodeSelector` (from `GET /nodes`), `LiveChart` (from `useWebSocket` -> `/live`),
  `HistoryPanel` (from `GET /readings`).

## Data flow

```
serial bytes  (0xAA55-framed; see protocol-spec.md)
  -> serial_reader: resync on SYNC, slice candidate frame, parse_frame()
  -> event_bus.publish(FrameEvent{ raw, crc_ok, node_id, seq_num, values, received_at })
       |-> frame_buffer  : append to the in-memory ring (every candidate frame)
       |-> db.writer     : if NOT crc_ok -> INSERT raw_frames;
       |                   if crc_ok -> UPSERT node, INSERT reading (idempotent)
       |-> ws.manager    : if crc_ok -> JSON to matching /live clients' outboxes
       `-> archive.writer: if crc_ok -> queue.Queue.put_nowait (one call; a
                           dedicated thread does the actual file write + fsync,
                           never the event loop)
```

Separately, `db.retention` runs hourly off the request path: it aggregates
`readings` rows older than `READINGS_FULL_RES_HOURS` into `readings_rollup`
buckets and deletes them. `GET /readings` merges the two tiers on read. This is
**not yet gated on the archive tier** -- see "Archive tier" below.

`values` holds the decoded keys `tof_mm`, `accel_mss`, `gyro_rads`
(see protocol-spec.md §3); `db.writer` flattens the vectors into the
`accel_{x,y,z}` / `gyro_{x,y,z}` columns. `ingest/serial_reader.py` also adds a
derived `tof_out_of_range` bool (raw `tof_mm` above `TOF_MAX_VALID_MM`, the
VL53L0X no-target sentinel) — carried on the bus and stored in
`readings.tof_out_of_range`. The wire format and both `protocol.py` modules are
unchanged.

`frame_buffer`, `db.writer`, `ws.manager` and `archive.writer` subscribe
**independently**. None imports pyserial or touches the port. If one is slow or
crashes, the others are unaffected -- each also gets its own named subscription
(`bus.subscribe("db-writer")`, etc.) so `GET /health`'s `bus.by_subscriber` can
attribute a drop to whichever one actually lagged, not just report a single
total.

## Archive tier

`backend/archive/writer.py` is the system of record: every CRC-valid reading is
appended, at full resolution, forever, independent of whatever
`READINGS_FULL_RES_HOURS` SQLite is configured to keep. SQLite's `readings`
table is a disposable, bounded cache in front of it -- the archive is what
makes shrinking that window (a deferred production step) safe.

**Why two threads.** `open()`/`write()`/`fsync()` are blocking syscalls, and
`fsync` has unbounded worst-case latency on a busy disk. Inline on the event
loop that would freeze the WebSocket broadcast and every HTTP handler, same as
serial I/O. So the async bus subscriber does exactly one
`queue.Queue.put_nowait()` per reading -- no serialising, no file handles, no
syscalls -- and a dedicated writer thread owns all file I/O.

**Layout.**

```
archive/spool/node=1/2026-09-03T13.ndjson                              <- open, being appended
archive/data/readings/node_id=1/date=2026-09-03/hour=13/part-0000.ndjson.gz
archive/data/readings/node_id=1/date=2026-09-03/hour=13/part-0001.ndjson.gz
archive/data/readings/node_id=1/date=2026-09-03/hour=13/_manifest.json
```

Live writes are newline-delimited JSON (a crash costs at most one truncated
line, unlike a columnar format that needs a valid footer). A finished hour is
gzip-compressed and handed to `archive/store.py`'s `ArchiveStore` seam
(`put`/`exists`/`get_bytes`/`uri`) -- `LocalStore` today; a cloud store later is
one more implementation of that interface, with nothing else in the codebase
changing. Hive-style partition keys (`node_id=`/`date=`/`hour=`) let a future
query engine (DuckDB, Athena) prune without listing every object.

**Multi-part, never overwritten.** An hour can be sealed more than once: a
restart force-closes whatever hour is currently open, even one that has not
finished, and the process resumes into the same hour afterward. Each seal is
its own numbered part rather than replacing the last one -- an earlier version
always wrote `part-0000.ndjson.gz` and a second restart's seal silently
destroyed the first (found live 2026-09-04 via `verify_archive.py`: a sealed
hour's row count shrank across restarts while SQLite held far more for the same
hour). The manifest is read-modify-write: the existing one (if any) is read
back through `store.get_bytes()` and the new part appended to its `parts` list;
`rows_written` / `drops_observed` / `complete` are aggregates over every part.

**Durability is honest, not optimistic.** The event bus drops events when a
subscriber lags -- correct behaviour, since blocking ingestion would be worse.
But a drop the archive did not notice would let it *lie*: compaction would
stamp the hour complete, retention (once gated on this) would trust that and
delete the SQLite copy, and the reading would be gone while everything reported
success. So every drop -- from the bus (`bus.dropped_for("archive")`) or the
writer's own internal queue -- writes a `_gap` record **into the spool file
itself**, out of band from the row queue so a full queue can never swallow the
notice that the queue is full. Any hour containing one is `complete: false`
forever. `complete: true` means **no loss observed by the backend**; nothing
stronger is knowable, since a frame lost over the air leaves only a `seq_num`
gap and no backend design recovers it (protocol-spec §6).

`seq_gaps_observed` records those RF-loss-shaped gaps separately and does not
affect `complete`. It is seeded across restarts too (`_recover_last_seq`, reading
the most recently sealed manifest's last `seq_num` per node back at startup) so
a gap landing exactly on a restart boundary is still counted, not just gaps
within one continuous run.

**Verification, not yet gating.** `scripts/verify_archive.py` reconciles the
archive against `readings` for the overlap window and verifies every sealed
manifest's checksum and row count. `db/retention.py` does **not** yet consult
manifests before deleting -- it still runs on its existing timer, deliberately,
until the archive has proven itself. `GET /readings` does not read the archive
either; it is write-only insurance for now. Both are the deferred next steps,
along with a cloud `ArchiveStore` implementation.

## Data model

| Table        | Purpose                        | Key columns                                                   | Constraints |
|--------------|--------------------------------|--------------------------------------------------------------|-------------|
| `nodes`      | known nodes                    | `node_id` PK, `last_seen`, `name` (nullable)                  | `stale` bool computed on read, never stored; `name` falls back to `"Node {id}"` on read when NULL |
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
(2 Hz) data stays *queryable* in `readings` before it is collapsed to 1-minute
avg/min/max rows and the originals are **permanently deleted from SQLite** (see
"Archive tier" above -- they are not gone from `archive/`, just no longer
queryable through the API). Cost is linear:

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

**Backups.** `scripts/backup_db.py` takes a consistent `VACUUM INTO` snapshot of
`noventis.db` safely while the backend runs (a raw copy of a live WAL DB can be
torn). Run it at least daily (cron / Task Scheduler). Fine-grained readings older
than `READINGS_FULL_RES_HOURS` stop being **queryable** through `GET /readings`
once rolled up -- but as of the archive tier (see above) they are **recoverable**
from `archive/`, just not yet queryable there (a deferred deep-history read tier
would restore that). For anything that must stay queryable, a backup still needs
to land inside the window. `archive/` is a second dataset now, and a single disk
copy until a cloud store is wired in -- back it up too for anything that must
not be lost outright, not just kept queryable.

The unique `(node_id, seq_num)` constraint makes re-ingestion (replaying a serial
capture, restarting the reader) idempotent *within a session* -- duplicate
readings are dropped with `ON CONFLICT DO NOTHING`.

A **node reboot** resets its `seq_num` to ~0, which would otherwise collide with
the prior session and freeze that node's readings. `db/writer.py` watches each
node's `seq_num`: a large backward jump (not the `0xFFFF -> 0x0000` wrap) after a
gap of silence is a restart, and it clears that node's `readings` so the new
session ingests. `writer.status().sessions_reset` counts how often it has fired.
(It does not touch `readings_rollup`; historical buckets survive a reset.)

Node liveness is derived on read, never stored: `GET /nodes` returns a boolean
`stale`, true when `now - last_seen` exceeds `NOVENTIS_STALE_AFTER_S`
(default 10 s). There is no online/offline enum and no `NODE_TIMEOUT`; the
`NodeOut` shape is `{node_id, name, last_seen, stale}`.

Persistence: SQLAlchemy async ORM, SQLite in **WAL mode** (concurrent
writer + API readers). No Alembic yet -- `Base.metadata.create_all` at startup.

## API contract

| Method | Path                        | Notes                                                   |
|--------|-----------------------------|--------------------------------------------------------|
| GET    | `/nodes`                    | all nodes + computed `stale` bool (see below)          |
| GET    | `/session`                  | `{session_start}` — when this backend run began; resets on restart, not persisted |
| GET    | `/readings?node_id=<id>`    | readings newest first, paginated; when `since` reaches past `READINGS_FULL_RES_HOURS` the older portion is served from `readings_rollup`, merged transparently (bucketed rows carry `rollup: true` + `sample_count`) |
| GET    | `/raw-frames?node_id=<id>`  | `raw_frames` table -- CRC-failed frames only now; `node_id` optional (NULL for bad header) |
| GET    | `/debug/frames`             | window onto the in-memory frame ring buffer (recent frames, CRC pass + fail); `limit`, `crc_ok`, `node_id` filters; empty after a restart |
| PATCH  | `/nodes/{node_id}`          | set display name; body `{"name": "<1..64 chars>"}`; returns the updated node (404 unknown node, 422 blank name) |
| POST   | `/rescan`                   | force serial CP2102 auto-detect to re-run; waits a bounded window and returns `{ok, connected, port, last_error}` |
| WS     | `/live?node_id=<id>`        | data message carries no `type` (`{node_id, seq_num, ts, values}`, `ts` ISO-8601); control messages do and unrecognised ones must be ignored: `{"type":"ready","node_id":…}` on connect, `{"type":"gap","dropped":n}` when this client's own outbox overflowed, `{"type":"ping","ts":…}` liveness to an idle client only; omit `node_id` for all nodes; beyond `MAX_WS_CONNECTIONS` (default 32) a new connection is refused with close code 1013 |

`GET /health` additionally reports `frame_buffer` (ring `size`/`capacity`/
`received_total`), `retention` (`passes`, `buckets_written`,
`readings_rolled_up`, `last_pass_at`, `auto_vacuum_mode`, …), `bus`
(`by_subscriber`: per-subscriber queued/delivered/dropped, keyed by the name
passed to `subscribe()`), `ws` (`connections`, `max_connections`,
`client_queue_max`, `messages_sent`/`dropped`, `connections_rejected`, per-client
detail) and `archive` (`rows_written`, `queued`, `open_hours`,
`segments_sealed`, `incomplete_hours`, `bus_drops`, `queue_drops`,
`spool_bytes`, `last_fsync_at`, …) so all are verifiable at a glance.

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
- A cloud `ArchiveStore` implementation (`LocalStore` is the only one today;
  the interface is the seam this is deferred behind).
- A read tier over the archive (e.g. DuckDB against the Parquet/NDJSON.gz
  segments) so `GET /readings` can reach past `READINGS_FULL_RES_HOURS` into
  full-resolution history instead of only the 1-minute rollup.
- Gating `db/retention.py`'s deletion on archive manifests being `complete`
  (today retention runs on its timer regardless; `verify_archive.py` is the
  interim way to trust the archive before wiring that gate).
- Lowering `READINGS_FULL_RES_HOURS` below 168 (safe only once the two items
  above exist -- until then SQLite is the only queryable copy of full-res data).
