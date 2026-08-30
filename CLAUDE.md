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
        |-> db.writer  : raw_frames (always) + nodes.last_seen + readings (CRC-ok, idempotent)
        `-> ws.manager : JSON push to /live clients (CRC-ok only), node_id-filtered

- `edge/protocol.py` == `backend/ingest/protocol.py` -- TLV + CRC-16-CCITT codec
  for the shipped `0xAA55` frame format, plus `extract_frames` stream de-framer
- `edge/node_tx.py` -- real production TX script, importing `protocol`
- `backend/legacy/base_rx.py` -- original standalone receiver, kept as the
  decoder reference (NOT imported by the app)
- `backend/ingest/serial_reader.py` -- CP2102 autodetect by USB VID:PID
  `10C4:EA60`, background thread, rescans when absent, never hardcodes a port
- `backend/ingest/event_bus.py` -- sync `publish()` / async `subscribe()` fan-out
- `backend/db/{models,session,writer}.py` -- async SQLAlchemy, SQLite WAL,
  `INSERT ... ON CONFLICT DO NOTHING` on `(node_id, seq_num)`
- `backend/ws/manager.py` -- `/live` connection registry + broadcast loop
- `backend/api/{nodes,readings,raw_frames}.py` -- the REST routers
- `backend/main.py` -- lifespan wiring + `/health`
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
   in-process asyncio pub/sub (per-subscriber `asyncio.Queue` fan-out).
   `backend/db/writer.py` and `backend/ws/manager.py` subscribe **independently**.
   Nothing outside `backend/ingest/` may import pyserial or touch the serial port.

5. **Data model:**
   - `nodes`: `node_id`, `last_seen`; `status` is **computed on read, never
     stored**.
   - `raw_frames`: every packet received, `crc_ok` pass/fail, nullable `node_id`
     -- a forensic log.
   - `readings`: CRC-valid decoded values only; index `(node_id, timestamp)`;
     **unique `(node_id, seq_num)`** for idempotent re-ingestion.

6. **Persistence:** SQLAlchemy **async** ORM against **SQLite in WAL mode**. No
   Alembic yet -- `Base.metadata.create_all` is fine at this stage.

7. **API contract:**
   - REST: `GET /nodes`, `GET /readings?node_id=...`, `GET /raw-frames?node_id=...`
   - WebSocket: `/live?node_id=...` (omit `node_id` to receive all nodes)

8. Keep this file and the two `@`-referenced docs in sync when any of the above
   changes, so future sessions don't have to be re-told.
