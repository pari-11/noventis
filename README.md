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
- **Decoupled consumers.** `db/writer.py`, `ws/manager.py`, `ingest/frame_buffer.py`
  and `archive/writer.py` each `subscribe()` to the bus independently; none
  imports pyserial, and one being slow or crashing never affects the others.
- **Forensic + clean split.** `raw_frames` keeps only CRC-*failed* frames (a
  tiny, permanent corruption log); recent CRC-valid frames live ~40 min in an
  in-memory ring (`GET /debug/frames`), never on disk. `readings` holds CRC-valid
  decoded values at full resolution for `READINGS_FULL_RES_HOURS` (default 7
  days), then `db/retention.py` rolls them into 1-minute `readings_rollup`
  buckets and deletes the originals. `GET /readings` merges both tiers.
- **Archive tier is the system of record.** `archive/writer.py` appends every
  CRC-valid reading, full resolution, forever, to `archive/` -- independent of
  whatever window SQLite keeps. `readings` is a disposable cache in front of it,
  not the last copy. All file I/O runs on a dedicated thread, never the event
  loop. See "Storage, retention & backups" below and
  [docs/architecture.md](docs/architecture.md#archive-tier).

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
| `NOVENTIS_READINGS_FULL_RES_HOURS` | `168` | how far back full-resolution (2 Hz) readings stay queryable through the API before they become 1-minute rollups; raise for longer queryable history at ~188 MB / node / 7 days. **Real per-node ceiling is `min(this, ~9.1 h)`** -- `seq_num`'s uint16 wrap. Not the same as durability -- see "Storage, retention & backups" |
| `NOVENTIS_RAW_FRAME_BUFFER_SIZE` | `5000` | size of the in-memory recent-frames ring behind `GET /debug/frames` (~40 min at 2 Hz) |
| `NOVENTIS_MAX_WS_CONNECTIONS` | `32` | `/live` connections beyond this are refused (close code 1013) |
| `NOVENTIS_WS_CLIENT_QUEUE_MAX` | `256` | per-connection outbox depth; a client that falls behind drops only its own oldest messages |
| `NOVENTIS_WS_PING_INTERVAL_S` | `20` | liveness ping to idle `/live` clients; keep below any reverse-proxy read timeout |
| `NOVENTIS_ARCHIVE_DIR` | `./archive` | root of the append-only archive tier (spool + sealed segments) |
| `NOVENTIS_ARCHIVE_STORE` | `local` | where sealed segments go; only `local` exists today (the cloud step is deferred) |
| `NOVENTIS_ARCHIVE_QUEUE_MAX` | `20000` | hand-off queue depth between the bus and the archive writer thread (~2.8 h at 2 Hz) |
| `NOVENTIS_ARCHIVE_FSYNC_INTERVAL_S` | `5` | how often the spool is forced to disk -- the data-loss window on a hard power cut |
| `NOVENTIS_ARCHIVE_HOUR_GRACE_S` | `60` | how long past the end of an hour to wait before sealing it, so a late frame isn't stranded |

### 2. Frontend

```
cd frontend
npm install
npm run dev            # http://localhost:5173 — proxies /health /nodes /readings /raw-frames /debug /live to :8000
```

### Protocol self-test (no dependencies)

```
python edge/protocol.py
```

### Keeping the two protocol modules in sync

`edge/protocol.py` and `backend/ingest/protocol.py` must stay **byte-for-byte
identical** (the project's single point of failure). Drift fails late and
misleadingly: the node keeps transmitting, the backend rejects every frame as
corrupt, and it presents as a radio fault rather than a code change.

A pre-commit hook refuses any commit that drifts them. **Install it once per
clone:**

```
git config core.hooksPath .githooks
```

```
python scripts/check_protocol_sync.py    # verify (exit 1 if drifted)
python scripts/sync_protocol.py          # repair: edge/ is the master copy
python scripts/sync_protocol.py --reverse   # if you edited the backend mirror
```

`edge/protocol.py` is the master copy — it is what ships to the hardware. The
check only enforces that the files match; a wire-format change must also update
[docs/protocol-spec.md](docs/protocol-spec.md) in the same commit.

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

## Storage, retention & backups

The dataset lives in two places now: `noventis.db` (SQLite, bounded, disposable)
and `archive/` (append-only, unbounded, the system of record). Understand these
four points before running in production:

1. **Full-resolution readings are kept *queryable* for `NOVENTIS_READINGS_FULL_RES_HOURS`
   (default 168 h = 7 days), then rolled up — but the real per-node ceiling is
   `min(that, ~9.1 h)`.** `seq_num` is uint16 and wraps every ~9.1 h at 2 Hz;
   `db/writer.py` clears a node's `readings` on that wrap (same as it does on a
   reboot) so ingestion never freezes — but it also means one continuously-running
   node can't exceed ~9.1 h of full-res data in SQLite, no matter how high the
   knob is set. `archive/` (point 2) is unaffected and is where longer
   full-resolution history actually lives. `backend/db/retention.py` still runs
   hourly for whatever *is* within the window: older readings are aggregated into
   1-minute per-node `readings_rollup` buckets (avg + min + max + count) and the
   original ~0.5 s rows are removed **from SQLite**; `GET /readings` transparently
   serves the rollup tier for older ranges (see
   [docs/architecture.md](docs/architecture.md#full-resolution-window--storage)).

2. **The archive tier keeps every reading, forever, regardless of that window.**
   `backend/archive/writer.py` independently appends every CRC-valid reading to
   `NOVENTIS_ARCHIVE_DIR` (default `./archive`) at full resolution — it does not
   care what `NOVENTIS_READINGS_FULL_RES_HOURS` is set to. SQLite's `readings`
   table is a disposable, bounded cache in front of it, not the last copy.
   `scripts/verify_archive.py` reconciles the two and verifies every sealed
   segment's checksum:

   ```
   python scripts/verify_archive.py             # last 24 h, all nodes
   python scripts/verify_archive.py --hours 168 --node-id 1 -v
   ```

   **Two things this does not yet do**, both deliberate for now: retention does
   not consult the archive before deleting from SQLite (it still runs on its own
   timer), and `GET /readings` does not read the archive back — data past the
   full-res window is *recoverable* from `archive/` but not yet *queryable*
   through the API. `archive/` is a single copy on this disk until a cloud store
   is wired in (deferred) — back it up alongside `noventis.db`, it is not covered
   by `scripts/backup_db.py`.

3. **Take backups — at least daily — with the provided script, not `cp`.**

   ```
   python scripts/backup_db.py                 # -> ./backups/noventis-YYYYMMDD-HHMMSSZ.db
   python scripts/backup_db.py --out-dir D:\noventis-backups
   ```

   It uses SQLite `VACUUM INTO` (a raw file copy of a live WAL-mode DB can be
   torn/stale), is safe to run while the backend is up, and verifies the
   snapshot (`quick_check` + row counts) before exiting 0. Schedule it via cron
   or Windows Task Scheduler.

   > ⚠️ **A `noventis.db` backup older than `NOVENTIS_READINGS_FULL_RES_HOURS`
   > only ever contains 1-minute rollups for the older period** — SQLite itself
   > never held more. The raw 2 Hz data for that period is not gone, though: it
   > is in `archive/` (point 2 above) if the archive tier was running at the
   > time. `noventis.db` backups are for *queryable* history; `archive/` is for
   > *not losing data at all*. Back up both.

4. **The backend never deletes data at startup.** A database created before the
   raw-frame storage change still carries its old CRC-valid `raw_frames` rows;
   clearing them (they are dead weight under the new model) is a deliberate,
   prompted operator action, not boot behaviour:

   ```
   python scripts/migrate_purge_legacy_frames.py            # shows the count, asks to confirm
   python scripts/migrate_purge_legacy_frames.py --dry-run  # count only
   python scripts/migrate_purge_legacy_frames.py --yes      # non-interactive
   ```

## Handover

Long-form hardware/wiring documentation: `docs/handover/Noventis Documentation.pdf`
(added out of band, git-ignored).
