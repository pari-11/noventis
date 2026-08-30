/**
 * HistoryPanel -- recent persisted readings for the selected node.
 *
 * Fetches GET /readings?node_id=<id>&limit=100 (newest first) on node change and
 * on a manual Refresh. Disabled when no node is selected (the endpoint requires
 * a node_id). Renders a compact table.
 */

import { useEffect, useState } from 'react'

type Reading = {
  id: number
  node_id: number
  seq_num: number
  timestamp: string
  tof_mm: number | null
  accel_x: number | null
  accel_y: number | null
  accel_z: number | null
  gyro_x: number | null
  gyro_y: number | null
  gyro_z: number | null
}

type Props = { nodeId: number | null }

const fmt = (v: number | null, d = 2) => (v == null ? '·' : v.toFixed(d))

export function HistoryPanel({ nodeId }: Props) {
  const [rows, setRows] = useState<Reading[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    if (nodeId == null) {
      setRows([])
      setError(null)
      return
    }
    let alive = true
    setLoading(true)
    fetch(`/readings?node_id=${nodeId}&limit=100`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json() as Promise<Reading[]>
      })
      .then((data) => {
        if (alive) {
          setRows(data)
          setError(null)
        }
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [nodeId, reload])

  return (
    <section className="card history">
      <header className="card-head">
        <h2>History {nodeId == null ? '' : `· node ${nodeId}`}</h2>
        <button
          className="chip"
          disabled={nodeId == null || loading}
          onClick={() => setReload((n) => n + 1)}
        >
          {loading ? 'loading…' : 'Refresh'}
        </button>
      </header>

      {nodeId == null && <p className="muted small">pick a node to see its history</p>}
      {error && <p className="error small">/readings: {error}</p>}
      {nodeId != null && !error && rows.length === 0 && !loading && (
        <p className="muted small">no readings yet</p>
      )}

      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>time</th>
                <th>seq</th>
                <th>ToF mm</th>
                <th>accel x/y/z (m/s²)</th>
                <th>gyro x/y/z (rad/s)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>{new Date(r.timestamp).toLocaleTimeString()}</td>
                  <td>{r.seq_num}</td>
                  <td>{r.tof_mm ?? '·'}</td>
                  <td>
                    {fmt(r.accel_x)} / {fmt(r.accel_y)} / {fmt(r.accel_z)}
                  </td>
                  <td>
                    {fmt(r.gyro_x, 3)} / {fmt(r.gyro_y, 3)} / {fmt(r.gyro_z, 3)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
