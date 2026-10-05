/**
 * App.tsx -- Noventis dashboard shell.
 *
 * Owns the selected node_id (null = all) and passes it to NodeSelector,
 * LiveChart and HistoryPanel. Polls GET /health every 5s and shows whether the
 * backend's LoRa serial port is currently connected.
 *
 * Also owns:
 *   - the light/dark theme (persisted to localStorage, applied as
 *     <html data-theme>), toggled from the header. Dark is the default.
 *   - the header "Rescan for nodes" control: POSTs /rescan (which re-runs the
 *     backend serial auto-detect) and drives a four-state machine
 *     idle -> reconnecting -> success | failure off the real response.
 *
 * node = "all"  -> grid of NodeSummaryCards (no chart, no history).
 * node selected -> LiveChart + HistoryPanel side by side.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { NodeSelector, type NodeInfo } from './components/NodeSelector'
import { LiveChart } from './components/LiveChart'
import { HistoryPanel } from './components/HistoryPanel'
import { NodeSummaryCard } from './components/NodeSummaryCard'
import { RawDataPage } from './components/RawDataPage'
import { NodePowerControl } from './components/NodePowerControl'

type Health = {
  status: string
  lora: {
    connected: boolean
    port: string | null
    baud: number
    frames_ok: number
    frames_bad: number
    last_error: string | null
  }
  nodes_seen: number
}

type RescanState = 'idle' | 'reconnecting' | 'success' | 'failure'

const Icon = ({ d, size = 15 }: { d: string; size?: number }) => (
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
  sun: 'M12 4V2m0 20v-2m8-8h2M2 12h2m13.7-5.7 1.4-1.4M4.9 19.1l1.4-1.4m11.4 0 1.4 1.4M4.9 4.9l1.4 1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  moon: 'M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z',
}

export default function App() {
  const [nodeId, setNodeId] = useState<number | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [healthErr, setHealthErr] = useState<string | null>(null)
  const [nodes, setNodes] = useState<NodeInfo[]>([])
  const [now, setNow] = useState(() => Date.now())

  // "View Raw data" is a hash route (#/raw) -- no router dependency, and the
  // browser Back button returns to the dashboard. The dashboard stays MOUNTED
  // (just hidden) while it's open, so its /live connection is left untouched.
  const [rawView, setRawView] = useState(() => window.location.hash === '#/raw')
  useEffect(() => {
    const onHash = () => setRawView(window.location.hash === '#/raw')
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  // Once opened, the raw page stays mounted (hidden when you go back) so its
  // stream and buffered packets survive a trip to the dashboard and back.
  const [rawOpened, setRawOpened] = useState(rawView)
  useEffect(() => {
    if (rawView) setRawOpened(true)
  }, [rawView])

  const [theme, setTheme] = useState<'light' | 'dark'>(
    () => (localStorage.getItem('noventis-theme') === 'light' ? 'light' : 'dark'),
  )
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('noventis-theme', theme)
  }, [theme])

  const [rescan, setRescan] = useState<RescanState>('idle')
  const rescanTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(rescanTimer.current), [])

  const doRescan = useCallback(async () => {
    setRescan('reconnecting')
    window.clearTimeout(rescanTimer.current)
    try {
      const res = await fetch('/rescan', { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = (await res.json()) as { ok?: boolean; connected?: boolean }
      setRescan((data.ok ?? data.connected) ? 'success' : 'failure')
    } catch {
      setRescan('failure')
    }
    rescanTimer.current = window.setTimeout(() => setRescan('idle'), 4000)
  }, [])

  useEffect(() => {
    let alive = true
    const load = () =>
      fetch('/health')
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`)
          return r.json() as Promise<Health>
        })
        .then((h) => {
          if (alive) {
            setHealth(h)
            setHealthErr(null)
          }
        })
        .catch((e) => {
          if (alive) setHealthErr(e instanceof Error ? e.message : String(e))
        })
    load()
    const id = window.setInterval(load, 5000)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [])

  // poll /nodes -- feeds the "all nodes" summary grid AND the selected node's
  // display name (shown in the LiveChart / HistoryPanel headers). Re-fetches
  // immediately on a node switch so a rename shows without waiting for the tick.
  useEffect(() => {
    let alive = true
    const load = () =>
      fetch('/nodes')
        .then((r) => (r.ok ? (r.json() as Promise<NodeInfo[]>) : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d) => {
          if (alive) setNodes(d)
        })
        .catch(() => {
          /* NodeSelector surfaces /nodes errors; the grid just waits */
        })
    load()
    const id = window.setInterval(load, 5000)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [nodeId])

  useEffect(() => {
    if (nodeId != null) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [nodeId])

  const lora = health?.lora
  const selectedName =
    nodeId == null ? undefined : nodes.find((n) => n.node_id === nodeId)?.name

  const rescanLabel =
    rescan === 'reconnecting'
      ? 'Rescanning…'
      : rescan === 'success'
        ? 'Rescan complete'
        : rescan === 'failure'
          ? 'No adapter found'
          : 'Rescan for nodes'

  return (
    <div className="app">
      <header className="app-header">
        <h1>Noventis</h1>
        <div className="conn">
          {healthErr ? (
            <span className="ws-badge closed">backend unreachable</span>
          ) : lora ? (
            <span className={lora.connected ? 'ws-badge open' : 'ws-badge closed'}>
              LoRa {lora.connected ? `connected · ${lora.port}` : 'disconnected'}
            </span>
          ) : (
            <span className="ws-badge">checking…</span>
          )}
          {health && <span className="muted small">{health.nodes_seen} node(s) seen</span>}
          {lora?.connected && (
            <span className="muted small">
              {lora.frames_ok} ok / {lora.frames_bad} bad
            </span>
          )}
          {lora && !lora.connected && lora.last_error && (
            <span className="muted small conn-msg">{lora.last_error}</span>
          )}
          <button
            className={rescan === 'reconnecting' ? 'btn ghost-btn spinning' : 'btn ghost-btn'}
            onClick={() => void doRescan()}
            disabled={rescan === 'reconnecting'}
            title="Rescan serial ports for the LoRa base station"
          >
            <Icon d={I.refresh} size={14} />
            {rescanLabel}
          </button>
        </div>
        <button
          className="btn raw-nav-btn"
          onClick={() => {
            if (rawView) setNodeId(null) // "← Dashboard" lands on the All nodes tab
            window.location.hash = rawView ? '' : '#/raw'
          }}
          title={rawView ? 'Back to the dashboard' : 'See every packet exactly as received, in hex'}
        >
          {rawView ? '← Dashboard' : 'View Raw data'}
        </button>
        <button
          className="btn icon-btn"
          onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
          title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          aria-label="Toggle colour theme"
        >
          <Icon d={theme === 'dark' ? I.sun : I.moon} size={18} />
        </button>
      </header>

      {rawOpened && (
        <div hidden={!rawView}>
          <RawDataPage initialNodeId={nodeId} />
        </div>
      )}

      <div hidden={rawView}>
      <NodeSelector value={nodeId} onChange={setNodeId} />
      {nodeId != null && <NodePowerControl key={nodeId} nodeId={nodeId} nodeName={selectedName} />}

      {nodeId == null ? (
        <main>
          <h2 className="section-title">All nodes</h2>
          {nodes.length === 0 ? (
            <p className="muted small">no nodes seen yet</p>
          ) : (
            <div className="summary-grid">
              {nodes.map((n) => (
                <NodeSummaryCard key={n.node_id} node={n} now={now} onSelect={setNodeId} />
              ))}
            </div>
          )}
        </main>
      ) : (
        <main className="grid">
          <LiveChart key={nodeId} nodeId={nodeId} nodeName={selectedName} />
          <HistoryPanel nodeId={nodeId} nodeName={selectedName} />
        </main>
      )}
      </div>
    </div>
  )
}
