/**
 * NodeSummaryCard -- one node's status at a glance for the "all nodes" view.
 *
 * Numbers only, no chart: live/stale badge (from GET /nodes' computed `stale`),
 * latest ToF + how long ago it landed, last seq_num, ok/total CRC ratio over the
 * last 500 raw frames, and last-seen time. The whole card is a button: clicking
 * it selects that node (same as NodeSelector).
 */

import { useEffect, useState } from 'react'
import type { NodeInfo } from './NodeSelector'
import type { Reading } from './HistoryPanel'

type FrameTally = { ok: number; total: number }

type Props = {
  node: NodeInfo
  now: number
  onSelect: (id: number) => void
}

const RAW_WINDOW = 500

const mag = (x: number | null, y: number | null, z: number | null): number | null =>
  x == null || y == null || z == null ? null : Math.sqrt(x * x + y * y + z * z)

/** Coarse "time since" label: 42s ago / 7m ago / 19h ago / 3d ago. */
const ago = (ms: number): string => {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

export function NodeSummaryCard({ node, now, onSelect }: Props) {
  const id = node.node_id
  const [latest, setLatest] = useState<Reading | null>(null)
  const [frames, setFrames] = useState<FrameTally | null>(null)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const [rRes, fRes] = await Promise.all([
          fetch(`/readings?node_id=${id}&limit=1`),
          fetch(`/raw-frames?node_id=${id}&limit=${RAW_WINDOW}`),
        ])
        if (alive && rRes.ok) {
          const rows = (await rRes.json()) as Reading[]
          setLatest(rows[0] ?? null)
        }
        if (alive && fRes.ok) {
          const rows = (await fRes.json()) as { crc_ok: boolean }[]
          const ok = rows.reduce((n, f) => n + (f.crc_ok ? 1 : 0), 0)
          setFrames({ ok, total: rows.length })
        }
      } catch {
        /* keep last-known values on a transient failure */
      }
    }
    load()
    const t = window.setInterval(load, 10000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [id])

  const agoMs = latest ? now - Date.parse(latest.timestamp) : null
  const agoLabel = agoMs == null ? null : ago(agoMs)
  const accelMag = latest ? mag(latest.accel_x, latest.accel_y, latest.accel_z) : null
  const gyroMag = latest ? mag(latest.gyro_x, latest.gyro_y, latest.gyro_z) : null
  // until the node is actually being heard from (not stale), the last stored
  // reading is history, not a live value -- show a placeholder instead.
  const detected = !node.stale

  return (
    <button className="summary-card card" onClick={() => onSelect(id)}>
      <div className="sc-top">
        <span className="sc-id">{node.name}</span>
        <span className={node.stale ? 'pill' : 'pill up'}>{node.stale ? 'Offline' : 'Online'}</span>
      </div>

      <div className="sc-tof">
        {!detected ? (
          <span className="sc-tof-val sc-tof-oor">--</span>
        ) : latest?.tof_out_of_range ? (
          <span className="sc-tof-val sc-tof-oor">out of range</span>
        ) : (
          <>
            <span className="sc-tof-val">{latest?.tof_mm ?? '--'}</span>
            <span className="sc-unit">mm TOF</span>
          </>
        )}
        {agoLabel && <span className="muted small sc-ago mono">{agoLabel}</span>}
      </div>

      <div className="sc-sensors mono small">
        <span>
          <span className="muted">|accel|</span> {accelMag == null ? '·' : accelMag.toFixed(2)}{' '}
          <span className="muted">m/s²</span>
        </span>
        <span>
          <span className="muted">|gyro|</span> {gyroMag == null ? '·' : gyroMag.toFixed(3)}{' '}
          <span className="muted">rad/s</span>
        </span>
      </div>

      <dl className="sc-meta">
        <div>
          <dt>seq</dt>
          <dd>{latest?.seq_num ?? '·'}</dd>
        </div>
        <div>
          <dt>frames ok</dt>
          <dd>{frames ? `${frames.ok}/${frames.total}` : '·'}</dd>
        </div>
      </dl>

      <span className="muted small mono">last seen {new Date(node.last_seen).toLocaleTimeString()}</span>
    </button>
  )
}
