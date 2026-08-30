/**
 * NodeSelector -- choose which node the dashboard is focused on.
 *
 * Polls GET /nodes every `pollMs` (default 5s) and renders one button per node
 * plus an "All nodes" button. Each node shows a live/stale dot from the API's
 * computed `stale` flag. `value === null` means "all".
 */

import { useEffect, useState } from 'react'

export type NodeInfo = {
  node_id: number
  last_seen: string
  stale: boolean
}

type Props = {
  value: number | null
  onChange: (id: number | null) => void
  pollMs?: number
}

export function NodeSelector({ value, onChange, pollMs = 5000 }: Props) {
  const [nodes, setNodes] = useState<NodeInfo[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const res = await fetch('/nodes')
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data: NodeInfo[] = await res.json()
        if (alive) {
          setNodes(data)
          setError(null)
        }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      }
    }
    load()
    const id = window.setInterval(load, pollMs)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [pollMs])

  return (
    <div className="node-selector">
      <button className={value === null ? 'chip active' : 'chip'} onClick={() => onChange(null)}>
        All nodes
      </button>

      {nodes.map((n) => (
        <button
          key={n.node_id}
          className={value === n.node_id ? 'chip active' : 'chip'}
          onClick={() => onChange(n.node_id)}
          title={`last seen ${new Date(n.last_seen).toLocaleString()}`}
        >
          <span className={n.stale ? 'dot stale' : 'dot live'} />
          Node {n.node_id}
        </button>
      ))}

      {nodes.length === 0 && !error && <span className="muted">no nodes seen yet</span>}
      {error && <span className="error">/nodes: {error}</span>}
    </div>
  )
}
