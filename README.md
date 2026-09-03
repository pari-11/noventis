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
- **Forensic + clean split.** `raw_frames` keeps only CRC-*failed* frames (a
  tiny, permanent corruption log); recent CRC-valid frames live ~40 min in an
  in-memory ring (`GET /debug/frames`), never on disk. `readings` holds CRC-valid
  decoded values at full resolution for `READINGS_FULL_RES_HOURS` (default 7
  days), then `db/retention.py` rolls them into 1-minute `readings_rollup`
  buckets and deletes the originals. `GET /readings` merges both tiers.

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
| `NOVENTIS_READINGS_FULL_RES_HOURS` | `168` | how far back full-resolution (2 Hz) readings stay queryable before they become 1-minute rollups; raise for longer fine-grained history at ~188 MB / node / 7 days |
| `NOVENTIS_RAW_FRAME_BUFFER_SIZE` | `5000` | size of the in-memory recent-frames ring behind `GET /debug/frames` (~40 min at 2 Hz) |

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

The entire dataset is a single SQLite file (`noventis.db` by default). Understand
these three points before running in production:

1. **Full-resolution readings are kept for `NOVENTIS_READINGS_FULL_RES_HOURS`
   (default 168 h = 7 days), then rolled up and deleted.** `backend/db/retention.py`
   runs hourly: readings older than that window are aggregated into 1-minute
   per-node `readings_rollup` buckets (avg + min + max + count) and the original
   ~0.5 s rows are **permanently removed**. `GET /readings` transparently serves
   the rollup tier for older ranges. Raise `NOVENTIS_READINGS_FULL_RES_HOURS` to
   keep fine-grained data longer — cost is linear, ≈188 MB per node per 7 days
   at 2 Hz (see [docs/architecture.md](docs/architecture.md#full-resolution-window--storage)).

2. **Take backups — at least daily — with the provided script, not `cp`.**

   ```
   python scripts/backup_db.py                 # -> ./backups/noventis-YYYYMMDD-HHMMSSZ.db
   python scripts/backup_db.py --out-dir D:\noventis-backups
   ```

   It uses SQLite `VACUUM INTO` (a raw file copy of a live WAL-mode DB can be
   torn/stale), is safe to run while the backend is up, and verifies the
   snapshot (`quick_check` + row counts) before exiting 0. Schedule it via cron
   or Windows Task Scheduler.

   > ⚠️ **Fine-grained readings older than the full-res window cannot be
   > reconstructed** once retention has rolled them up. If the raw 2 Hz data
   > matters, a backup must be taken **within `NOVENTIS_READINGS_FULL_RES_HOURS`**
   > of the data being recorded. Backups outside that window only ever contain
   > the 1-minute rollups for the older period.

3. **The backend never deletes data at startup.** A database created before the
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
