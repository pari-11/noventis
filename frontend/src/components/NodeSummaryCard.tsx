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
  const agoLabel = agoMs == null ? null : `${Math.max(0, Math.round(agoMs / 1000))}s ago`

  return (
    <button className="summary-card card" onClick={() => onSelect(id)}>
      <div className="sc-top">
        <span className="sc-id">Node {id}</span>
        <span className={node.stale ? 'sc-badge' : 'sc-badge live'}>
          <span className={node.stale ? 'dot stale' : 'dot live'} />
          {node.stale ? 'stale' : 'live'}
        </span>
      </div>

      <div className="sc-tof">
        {latest?.tof_out_of_range ? (
          <span className="sc-tof-val sc-tof-oor">out of range</span>
        ) : (
          <>
            <span className="sc-tof-val">{latest?.tof_mm ?? '·'}</span>
            <span className="sc-unit">mm</span>
          </>
        )}
        {agoLabel && <span className="muted small sc-ago">{agoLabel}</span>}
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
        <div>
          <dt>last seen</dt>
          <dd>{new Date(node.last_seen).toLocaleTimeString()}</dd>
        </div>
      </dl>
    </button>
  )
}
