# Noventis -- LoRa Telemetry Platform

Multi-node LoRa telemetry: battery edge nodes read ToF + IMU sensors and transmit
TLV-encoded frames over LoRa; a gateway radio feeds them to the backend over USB
serial; the backend validates, stores, and streams them to a React dashboard.

Full design and data flow: @docs/architecture.md
On-wire format (source of truth): @docs/protocol-spec.md

## Project stage: IMPLEMENTED (end to end)

The full pipeline is built and covered by integration tests
(`scratchpad/test_layer*.py` while under development):

    CP2102 serial (thread) -> protocol.parse_frame -> EventBus (fan-out)
        |-> frame_buffer   : in-memory ring of recent frames (every candidate; GET /debug/frames)
        |-> db.writer      : raw_frames (CRC-FAIL only) + nodes.last_seen + readings (CRC-ok, idempotent)
        |-> ws.manager     : JSON push to /live clients (CRC-ok only), node_id-filtered,
        |                    per-connection outbox so one slow client can't stall the rest
        `-> archive.writer : every CRC-valid reading, full resolution, forever, to
                             archive/ (own writer thread; system of record -- see constraint #8)

    db.retention (hourly, off the request path): readings older than
      READINGS_FULL_RES_HOURS -> readings_rollup buckets (avg+min+max), originals
      deleted; incremental_vacuum. GET /readings merges both tiers on read.
      NOT yet gated on the archive tier -- see constraint #8.

- `edge/protocol.py` == `backend/ingest/protocol.py` -- TLV + CRC-16-CCITT codec
  for the shipped `0xAA55` frame format, plus `extract_frames` stream de-framer
- `edge/node_tx.py` -- real production TX script, importing `protocol`
- `backend/legacy/base_rx.py` -- original standalone receiver, kept as the
  decoder reference (NOT imported by the app)
- `backend/ingest/serial_reader.py` -- CP2102 autodetect by USB VID:PID
  `10C4:EA60`, background thread, rescans when absent, never hardcodes a port
- `backend/ingest/event_bus.py` -- sync `publish()` / async `subscribe()` fan-out
- `backend/ingest/frame_buffer.py` -- bounded in-memory ring (`RAW_FRAME_BUFFER_SIZE`,
  default 5000) of recent candidate frames; independent bus subscriber, never
  persisted, empty after restart
- `backend/db/{models,session,writer}.py` -- async SQLAlchemy, SQLite WAL,
  `INSERT ... ON CONFLICT DO NOTHING` on `(node_id, seq_num)`
- `backend/db/retention.py` -- hourly readings rollup + retention (single tier),
  thresholds as named constants at the top
- `backend/ws/manager.py` -- `/live` connection registry; per-connection outbox
  + send task (one slow client can never stall the others); `{"type":"gap"}` /
  `{"type":"ping"}` control messages alongside the unchanged data-frame contract
- `backend/archive/writer.py` -- fourth independent bus subscriber; append-only
  archive, full resolution, forever (see constraint #8)
- `backend/archive/store.py` -- `ArchiveStore` seam (`LocalStore` today; a cloud
  store later is one more implementation, nothing else in the codebase changes)
- `backend/api/{nodes,readings,raw_frames,debug}.py` -- the REST routers
- `backend/main.py` -- lifespan wiring + `/health`
- `scripts/backup_db.py` -- consistent snapshot (`VACUUM INTO`) while the backend
  runs; `scripts/migrate_purge_legacy_frames.py` -- deliberate, prompted one-off
  cleanup of pre-model-change CRC-valid `raw_frames` rows;
  `scripts/verify_archive.py` -- reconciles `archive/` against `readings`, and
  verifies every sealed manifest's checksum and row count
- `scripts/check_protocol_sync.py` / `scripts/sync_protocol.py` -- enforce and
  repair constraint #2; wired into `.githooks/pre-commit`
  (`git config core.hooksPath .githooks` once per clone)
- `frontend/src/*` -- `useWebSocket` (backoff reconnect), `NodeSelector`,
  `LiveChart` (dependency-free SVG sparklines), `HistoryPanel`, `App`

**Still stubbed:** `edge/sensors/{tof,imu}.py` (node_tx talks to the drivers
directly today; these are extraction targets).

Run: `uvicorn backend.main:app --port 8000` (repo root) + `npm run dev` in
`frontend/`. No Alembic -- `init_db()` runs `create_all` on startup.

## Non-negotiable constraints (do not re-litigate without asking)

1. **`edge/node_tx.py` is production code.** Do **not** rewrite its sensor-read
   or LoRa-transmit logic. The TLV encoding + CRC-16 have already been pulled out
   into `edge/protocol.py` and are imported; keep it that way.

2. **Two protocol modules, one spec.** `edge/protocol.py` and
   `backend/ingest/protocol.py` must stay **byte-for-byte identical** (tag values,
   byte layout, CRC parameters, scale factors). `docs/protocol-spec.md` is the
   source of truth and describes the format the deployed firmware already speaks
   (`0xAA55` sync, tag `0x01` ToF / `0x02` IMU 6-axis). Change the spec and both
   modules together, in one commit. This is the project's single point of failure.

3. **Serial I/O must never block the FastAPI event loop.**
   `backend/ingest/serial_reader.py` runs the pyserial loop in a background
   thread / `asyncio.to_thread` and pushes CRC-checked, decoded frames onto the
   event bus.

4. **The event bus is the only coupling.** `backend/ingest/event_bus.py` is an
   in-process asyncio pub/sub (per-subscriber `asyncio.Queue` fan-out, named via
   `subscribe("<name>")` so drops are attributable, not just totalled).
   `backend/db/writer.py`, `backend/ws/manager.py`,
   `backend/ingest/frame_buffer.py` and `backend/archive/writer.py` subscribe
   **independently**. Nothing outside `backend/ingest/` may import pyserial or
   touch the serial port.

5. **Data model:**
   - `nodes`: `node_id`, `last_seen`, `name` (nullable, operator-set via
     `PATCH /nodes/{id}`; renders as `"Node {id}"` on read when NULL). A boolean
     `stale` (last frame older than `NOVENTIS_STALE_AFTER_S`, default 10 s) is
     **computed on read, never stored** -- there is no online/offline enum and
     no `NODE_TIMEOUT`.
   - `raw_frames`: **CRC-failed frames only**, nullable `node_id` -- a small
     forensic log, left unpruned (~12/24k). CRC-valid frames are **not**
     persisted: the most recent `RAW_FRAME_BUFFER_SIZE` (default 5000) live in
     `ingest/frame_buffer.py`'s in-memory ring, exposed by `GET /debug/frames`,
     and are gone on restart. `db/writer.py` only `INSERT`s here when
     `crc_ok` is False. A DB from before this model change still carries its old
     CRC-valid rows; the backend **never** deletes them at boot -- clearing them
     is a deliberate `python scripts/migrate_purge_legacy_frames.py` (prompts,
     prints the count first).
   - `readings`: CRC-valid decoded values, **full resolution for the last
     `READINGS_FULL_RES_HOURS`** (default **168** = 7 days; this is *the* knob to
     tune for how far back 0.5 s data stays queryable before it becomes 1-minute
     rollups -- linear storage cost, ~156 B/row, ~188 MB per node per 7 days at
     2 Hz); index `(node_id, timestamp)`;
     **unique `(node_id, seq_num)`** for idempotent re-ingestion *within a
     session*. A node reboot resets `seq_num` to ~0 and would collide with the
     prior session (readings freeze); `db/writer.py` detects the backward-jump +
     silence and clears that node's prior `readings` so the new session ingests
     (`raw_frames`/`readings_rollup` untouched; `status().sessions_reset` counts it).
     Includes a derived `tof_out_of_range` bool (raw `tof_mm` above
     `TOF_MAX_VALID_MM`, the VL53L0X no-target sentinel) computed in
     `ingest/serial_reader.py` -- **not** in the codec, so the two `protocol.py`
     modules stay identical (see #2).
   - `readings_rollup`: per-node `ROLLUP_INTERVAL_MINUTES` (default 1) time
     buckets of `readings` older than `READINGS_FULL_RES_HOURS`; stores
     **avg + min + max** (min/max deliberately kept so a bucket average never
     hides a short accel/gyro spike) and `sample_count`; **unique
     `(node_id, bucket_start)`**. `db/retention.py` fills it hourly, deletes the
     rolled-up originals, and runs `PRAGMA incremental_vacuum`
     (`auto_vacuum=INCREMENTAL`, converted once via a full `VACUUM` in
     `_prepare` -- the only thing `_prepare` does; it never deletes rows).
     **Single tier** -- do not add a coarser second rollup without asking.
     Rolled-up fine-grained data stops being **queryable** through
     `GET /readings` once it ages out of this window -- but since the archive
     tier (constraint #8) it is **recoverable** from `archive/`, just not yet
     queryable there (that needs a deferred deep-history read tier). For
     anything that must stay *queryable*, still back up within the window
     (`scripts/backup_db.py`); `archive/` is a single disk copy until a cloud
     store is wired in, so back it up too for anything that must not be lost.

6. **Persistence:** SQLAlchemy **async** ORM against **SQLite in WAL mode**. No
   Alembic yet -- `Base.metadata.create_all` is fine at this stage.

7. **API contract:**
   - REST: `GET /nodes`, `GET /readings?node_id=...`, `GET /raw-frames?node_id=...`,
     `GET /session` (returns `{session_start}` -- when this backend run began;
     resets on restart, not persisted)
   - `GET /readings` transparently merges `readings` + `readings_rollup`: when
     `since` reaches past `READINGS_FULL_RES_HOURS` the older portion comes from
     the rollup tier (rows flagged `rollup: true`, with `sample_count`; values
     are the bucket average). Callers don't pick a table.
   - `GET /debug/frames?limit=&crc_ok=&node_id=` -- window onto the in-memory
     frame ring buffer; same fields as `/raw-frames` except the row id is `seq`
     (the ring's monotonic counter), not `id`. `limit` caps at
     `RAW_FRAME_BUFFER_SIZE`, not 2000.
   - `GET /health` also reports `frame_buffer` (size/capacity), `retention`
     (passes, buckets_written, readings_rolled_up, auto_vacuum_mode, ...),
     `bus` (`by_subscriber`: per-subscriber queued/delivered/dropped, keyed by
     the name passed to `subscribe()`), `ws` (connections, max_connections,
     client_queue_max, messages_sent/dropped, connections_rejected, per-client
     detail) and `archive` (rows_written, queued, open_hours, segments_sealed,
     incomplete_hours, bus_drops, queue_drops, spool_bytes, last_fsync_at, ...).
   - `PATCH /nodes/{node_id}` -- body `{"name": "..."}`, sets the display name,
     returns the updated node
   - `POST /rescan` -- re-runs the `serial_reader` CP2102 auto-detect
     (`SerialReader.request_rescan()`), waits a bounded window, returns
     `{ok, connected, port, last_error}`. Does not touch pyserial itself.
   - WebSocket: `/live?node_id=...` (omit `node_id` to receive all nodes).
     Beyond `MAX_WS_CONNECTIONS` (default 32) a new connection is refused with
     close code 1013. A data message carries no `type` key (unchanged contract:
     `{node_id, seq_num, ts, values}`, `ts` an ISO-8601 string); every **control**
     message has one and unrecognised types must be ignored by the client:
     `{"type":"ready","node_id":...}` once on connect,
     `{"type":"gap","dropped":n}` when this client's own outbox overflowed (it
     missed `n` messages -- break the series there, don't interpolate across it),
     `{"type":"ping","ts":...}` (liveness, sent only to a client whose outbox is
     already empty). Each connection has its own bounded outbox
     (`WS_CLIENT_QUEUE_MAX`, default 256); one slow client drops only its own
     oldest messages and never blocks the broadcast to anyone else.

8. **Archive tier (system of record).** `backend/archive/writer.py` is a fourth
   independent bus subscriber (`subscribe("archive")`, constraint #4). Every
   CRC-valid reading is appended, at full resolution, forever, to
   `archive/spool/node=<id>/<hour>.ndjson` (newline-delimited JSON; a crash costs
   at most one truncated line). All file I/O, `fsync` included, runs on a
   dedicated writer thread, never the event loop (constraint #3) -- the async
   side does one `queue.Queue.put_nowait` per reading and nothing else.

   A finished hour is gzip-compressed and stored under
   `archive/data/readings/node_id=<id>/date=<date>/hour=<hour>/` via
   `backend/archive/store.py`'s `ArchiveStore` seam (`put`/`exists`/
   `get_bytes`/`uri`; `LocalStore` today, a cloud store later is one more
   implementation with nothing else in the codebase changing). An hour can be
   sealed more than once -- a restart force-closes whatever hour is open even if
   unfinished -- so each seal is its own numbered part
   (`part-0000.ndjson.gz`, `part-0001.ndjson.gz`, ...) and **never overwrites a
   prior part**. The sidecar `_manifest.json` next to the data (never a row in
   SQLite, so deleting the database can never destroy the record of what was
   archived) aggregates `rows_written` / `drops_observed` / `complete` across
   every part.

   `complete: true` means **no loss observed by the backend** -- nothing
   stronger is knowable; a frame lost over the air leaves only a `seq_num` gap
   (protocol-spec section 6, unrecoverable here). A bus or internal-queue drop
   writes a `_gap` record **into the spool file itself** (survives a crash that
   would erase an in-memory counter); any hour containing one is
   `complete: false` forever. `seq_gaps_observed` is tracked and restored across
   restarts (`_recover_last_seq`) but deliberately does NOT clear `complete`
   (usually RF loss, not backend fault).

   **Not yet gating retention or the read path.** `db/retention.py` still
   deletes on its existing timer; `scripts/verify_archive.py` reconciles the
   archive against `readings` and verifies every manifest, so the archive can be
   trusted before that gate is added (a deferred production step). Nothing in
   `GET /readings` reads the archive yet -- it is write-only insurance for now,
   and `READINGS_FULL_RES_HOURS` has deliberately NOT been lowered from 168 for
   this reason.

9. Keep this file and the two `@`-referenced docs in sync when any of the above
   changes, so future sessions don't have to be re-told.
