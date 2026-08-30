/**
 * App.tsx -- Noventis dashboard shell.
 *
 * Owns the selected node_id (null = all) and passes it to NodeSelector,
 * LiveChart and HistoryPanel. Polls GET /health every 5s and shows whether the
 * backend's LoRa serial port is currently connected.
 */

import { useEffect, useState } from 'react'
import { NodeSelector } from './components/NodeSelector'
import { LiveChart } from './components/LiveChart'
import { HistoryPanel } from './components/HistoryPanel'

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

export default function App() {
  const [nodeId, setNodeId] = useState<number | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [healthErr, setHealthErr] = useState<string | null>(null)

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

  const lora = health?.lora

  return (
    <div className="app">
      <header className="app-header">
        <h1>Noventis</h1>
        <div className="conn">
          {healthErr ? (
            <span className="pill down">backend unreachable</span>
          ) : lora ? (
            <span className={lora.connected ? 'pill up' : 'pill down'}>
              LoRa {lora.connected ? `connected · ${lora.port}` : 'disconnected'}
            </span>
          ) : (
            <span className="pill">checking…</span>
          )}
          {health && <span className="muted small">{health.nodes_seen} node(s) seen</span>}
          {lora?.connected && (
            <span className="muted small">
              {lora.frames_ok} ok / {lora.frames_bad} bad
            </span>
          )}
          {lora && !lora.connected && lora.last_error && (
            <span className="muted small">{lora.last_error}</span>
          )}
        </div>
      </header>

      <NodeSelector value={nodeId} onChange={setNodeId} />

      <main className="grid">
        <LiveChart nodeId={nodeId} />
        <HistoryPanel nodeId={nodeId} />
      </main>
    </div>
  )
}
