# Noventis -- LoRa Telemetry Platform

Multi-node LoRa telemetry: battery edge nodes read ToF + IMU sensors and transmit
TLV-encoded frames over LoRa; a gateway radio feeds them to the backend over USB
serial; the backend validates, stores, and streams them to a React dashboard.

Full design and data flow: @docs/architecture.md
On-wire format (source of truth): @docs/protocol-spec.md

## Project stage: SCAFFOLD

Most backend and frontend files are intentionally **stubs** -- module docstring +
TODO list, no implementation. Do not treat missing logic as a bug.

**Implemented** (small, fully specified, central -- keep them working):
- `edge/protocol.py` and `backend/ingest/protocol.py` (identical mirror)
- `backend/db/models.py`, `backend/db/session.py`
- `backend/ingest/event_bus.py`

**Stubbed** (fill in when asked): `edge/node_tx.py`, `edge/sensors/*`,
`backend/main.py`, `backend/ingest/serial_reader.py`, `backend/db/writer.py`,
`backend/ws/manager.py`, `backend/api/*`, all of `frontend/src/*`.

## Non-negotiable constraints (do not re-litigate without asking)

1. **`edge/node_tx.py` is production code.** The user pastes in their existing,
   already-working script. Do **not** rewrite its sensor-read or LoRa-transmit
   logic. The only permitted refactor: pull inline TLV encoding + CRC-16 out into
   `edge/protocol.py` and import it.

2. **Two protocol modules, one spec.** `edge/protocol.py` and
   `backend/ingest/protocol.py` must stay **byte-for-byte identical** (tag values,
   byte layout, CRC parameters). `docs/protocol-spec.md` is the source of truth.
   Change the spec and both modules together, in one commit. This is the
   project's single point of failure.

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
