/**
 * HistoryPanel -- persisted readings for the selected node, bounded to the
 * backend's CURRENT session.
 *
 * On every mount (a node switch does NOT remount this, but a full browser
 * refresh does) it:
 *   1. fetches GET /session -> { session_start }  (once per mount only)
 *   2. fetches GET /readings?node_id=<id>&since=<session_start>&limit=100
 *
 * `session_start` is never cached or persisted -- a refresh always re-fetches it
 * so the shown range tracks the backend's actual current run (a backend restart
 * resets it). The readings fetch also runs on node change, the manual Refresh
 * button, and a 25s auto-refresh interval; auto-refresh polls /readings ONLY,
 * never /session. Older sessions' rows stay in the DB, just outside this filter.
 *
 * Sensor filter (All / ToF / |accel| / |gyro|): a client-side view control that
 * shows/hides the corresponding table columns. The CSV export honours it --
 * only the currently-visible columns are serialised.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

const AUTO_REFRESH_MS = 25000

export type Reading = {
  id: number
  node_id: number
  seq_num: number
  timestamp: string
  tof_mm: number | null
  tof_out_of_range: boolean | null
  accel_x: number | null
  accel_y: number | null
  accel_z: number | null
  gyro_x: number | null
  gyro_y: number | null
  gyro_z: number | null
}

type Props = { nodeId: number | null; nodeName?: string }

type SensorFilter = 'all' | 'tof' | 'accel' | 'gyro'

const FILTERS: { key: SensorFilter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'tof', label: 'ToF' },
  { key: 'accel', label: '|accel|' },
  { key: 'gyro', label: '|gyro|' },
]

const fmt = (v: number | null, d = 2) => (v == null ? '·' : v.toFixed(d))

const Icon = ({ d, size = 14 }: { d: string; size?: number }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d={d} />
  </svg>
)
const I = {
  refresh: 'M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6',
  download: 'M12 3v12m0 0 4-4m-4 4-4-4M4 20h16',
}

export function HistoryPanel({ nodeId, nodeName }: Props) {
  const [rows, setRows] = useState<Reading[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [pulsing, setPulsing] = useState(false)
  const [filter, setFilter] = useState<SensorFilter>('all')
  // start of the backend's current run. Fetched fresh on every mount, never
  // stored -- until it resolves, the readings fetch holds off.
  const [sessionStart, setSessionStart] = useState<string | null>(null)

  // Every fetch gets a generation number; only the newest one is allowed to
  // write state. This makes a superseded request (node switched, or a second
  // click) a no-op instead of a stale overwrite -- and guarantees `loading`
  // always clears for the request that is actually current.
  const gen = useRef(0)
  const pulseTimer = useRef<number | undefined>(undefined)

  const showTof = filter === 'all' || filter === 'tof'
  const showAccel = filter === 'all' || filter === 'accel'
  const showGyro = filter === 'all' || filter === 'gyro'

  // (1) once per mount: the backend's current session_start. Not re-fetched by
  // auto-refresh -- it can't change while the backend run is alive.
  useEffect(() => {
    let alive = true
    fetch('/session')
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json() as Promise<{ session_start: string }>
      })
      .then((d) => {
        if (alive) setSessionStart(d.session_start)
      })
      .catch((e) => {
        if (alive) setError(`/session: ${e instanceof Error ? e.message : String(e)}`)
      })
    return () => {
      alive = false
    }
  }, [])

  // (2) readings for the selected node, from session_start onward
  const load = useCallback(async () => {
    if (nodeId == null || sessionStart == null) return
    const mine = ++gen.current
    setLoading(true)
    try {
      const qs = `node_id=${nodeId}&limit=500&since=${encodeURIComponent(sessionStart)}`
      const res = await fetch(`/readings?${qs}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = (await res.json()) as Reading[]
      if (mine !== gen.current) return
      setRows(data.slice()) // new array reference so React re-renders
      setError(null)
    } catch (e) {
      if (mine === gen.current) setError(`/readings: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      if (mine === gen.current) {
        setLoading(false)
        setPulsing(true)
        window.clearTimeout(pulseTimer.current)
        pulseTimer.current = window.setTimeout(() => setPulsing(false), 600)
      }
    }
  }, [nodeId, sessionStart])

  // initial load (fires once session_start resolves) + reload on node change
  useEffect(() => {
    setRows([])
    void load()
  }, [load])

  // auto-refresh on an interval while a node is selected -- readings only
  useEffect(() => {
    if (nodeId == null) return
    const id = window.setInterval(() => void load(), AUTO_REFRESH_MS)
    return () => window.clearInterval(id)
  }, [nodeId, load])

  useEffect(() => () => window.clearTimeout(pulseTimer.current), [])

  // CSV of the currently-loaded rows, honouring the active sensor filter:
  // only the visible columns are serialised.
  const exportCsv = useCallback(() => {
    if (rows.length === 0) return
    const head = ['time', 'seq']
    if (showTof) head.push('tof_mm', 'tof_out_of_range')
    if (showAccel) head.push('accel_x', 'accel_y', 'accel_z')
    if (showGyro) head.push('gyro_x', 'gyro_y', 'gyro_z')
    const cell = (v: number | null) => (v == null ? '' : String(v))
    const body = rows.map((r) => {
      const c: string[] = [new Date(r.timestamp).toISOString(), String(r.seq_num)]
      if (showTof) c.push(cell(r.tof_mm), r.tof_out_of_range ? 'true' : 'false')
      if (showAccel) c.push(cell(r.accel_x), cell(r.accel_y), cell(r.accel_z))
      if (showGyro) c.push(cell(r.gyro_x), cell(r.gyro_y), cell(r.gyro_z))
      return c.join(',')
    })
    const url = URL.createObjectURL(
      new Blob([[head.join(','), ...body].join('\n')], { type: 'text/csv' }),
    )
    const a = document.createElement('a')
    a.href = url
    a.download = `noventis-node-${nodeId}-session.csv`
    a.click()
    URL.revokeObjectURL(url)
  }, [rows, nodeId, showTof, showAccel, showGyro])

  return (
    <section className="card history">
      <header className="card-head">
        <h2>
          Current session history · {nodeId == null ? 'all nodes' : (nodeName ?? `Node ${nodeId}`)}
        </h2>
        <div className="history-actions">
          {nodeId != null && (
            <span
              className={pulsing ? 'auto-tag pulsing' : 'auto-tag'}
              title={`auto-refreshing every ${AUTO_REFRESH_MS / 1000}s`}
            >
              <span className="dot" /> auto
            </span>
          )}
          <button className="btn" disabled={nodeId == null || loading} onClick={() => void load()}>
            <Icon d={I.refresh} />
            {loading ? 'loading…' : 'Refresh'}
          </button>
          <button className="btn" disabled={rows.length === 0} onClick={exportCsv}>
            <Icon d={I.download} />
            Export CSV
          </button>
        </div>
      </header>

      {nodeId != null && (
        <div className="hist-filter">
          <div className="seg" role="group" aria-label="sensor column filter">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                className={filter === f.key ? 'on' : ''}
                aria-pressed={filter === f.key}
                onClick={() => setFilter(f.key)}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {nodeId == null && <p className="muted small">pick a node to see its history</p>}
      {error && <p className="error small">{error}</p>}
      {nodeId != null && !error && rows.length === 0 && !loading && (
        <p className="muted small">no readings yet this session</p>
      )}

      {rows.length > 0 && (
        <>
          <div className="table-wrap history-scroll">
            <table>
              <thead>
                <tr>
                  <th>time</th>
                  <th>seq</th>
                  {showTof && <th>ToF mm</th>}
                  {showAccel && <th>accel x/y/z (m/s²)</th>}
                  {showGyro && <th>gyro x/y/z (rad/s)</th>}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td>{new Date(r.timestamp).toLocaleTimeString()}</td>
                    <td>{r.seq_num}</td>
                    {showTof && (
                      <td>
                        {r.tof_out_of_range ? (
                          <span className="muted">out of range</span>
                        ) : (
                          (r.tof_mm ?? '·')
                        )}
                      </td>
                    )}
                    {showAccel && (
                      <td className="muted">
                        {fmt(r.accel_x)} / {fmt(r.accel_y)} / {fmt(r.accel_z)}
                      </td>
                    )}
                    {showGyro && (
                      <td className="muted">
                        {fmt(r.gyro_x, 3)} / {fmt(r.gyro_y, 3)} / {fmt(r.gyro_z, 3)}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted small">
            {rows.length} reading{rows.length === 1 ? '' : 's'} this session
            {rows.length >= 500 ? ' (newest 500 — scroll for older)' : ' — scroll for older'}
          </p>
        </>
      )}
    </section>
  )
}
