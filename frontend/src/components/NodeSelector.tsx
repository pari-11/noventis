/**
 * NodeSelector -- choose which node the dashboard is focused on.
 *
 * Polls GET /nodes every `pollMs` (default 5s) and renders one tab per node
 * plus an "All nodes" tab. Each node shows a live/stale dot from the API's
 * computed `stale` flag. `value === null` means "all".
 *
 * Rename: each node tab carries a pencil button -> clicking it swaps the label
 * for an inline <input> (focused, existing text selected). Enter or blur saves
 * the new name via PATCH /nodes/{id}; Escape cancels. The local list is updated
 * optimistically from the PATCH response so the new name shows immediately
 * (the next poll would refresh it anyway).
 */

import { useEffect, useRef, useState } from 'react'

export type NodeInfo = {
  node_id: number
  name: string
  last_seen: string
  stale: boolean
}

type Props = {
  value: number | null
  onChange: (id: number | null) => void
  pollMs?: number
}

const PencilIcon = ({ size = 12 }: { size?: number }) => (
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
    <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
  </svg>
)

export function NodeSelector({ value, onChange, pollMs = 5000 }: Props) {
  const [nodes, setNodes] = useState<NodeInfo[]>([])
  const [error, setError] = useState<string | null>(null)

  const [editing, setEditing] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  const [saveError, setSaveError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  // guards blur-save from firing a second time after Enter / Escape already
  // resolved the edit
  const resolved = useRef(false)

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

  useEffect(() => {
    if (editing != null && inputRef.current) {
      inputRef.current.focus()
      inputRef.current.select()
    }
  }, [editing])

  const startEdit = (n: NodeInfo) => {
    resolved.current = false
    setSaveError(null)
    setDraft(n.name)
    setEditing(n.node_id)
  }

  const cancelEdit = () => {
    resolved.current = true
    setEditing(null)
  }

  const commitEdit = async () => {
    if (editing == null || resolved.current) return
    resolved.current = true
    const id = editing
    const name = draft.trim()
    const current = nodes.find((n) => n.node_id === id)
    setEditing(null)
    if (!name || name === current?.name) return
    try {
      const res = await fetch(`/nodes/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const updated = (await res.json()) as NodeInfo
      setNodes((prev) => prev.map((n) => (n.node_id === id ? { ...n, name: updated.name } : n)))
      setSaveError(null)
    } catch (e) {
      setSaveError(`rename node ${id}: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  return (
    <div className="node-tabs">
      <button className={value === null ? 'node-tab active' : 'node-tab'} onClick={() => onChange(null)}>
        All nodes
      </button>

      {nodes.map((n) => (
        <span
          key={n.node_id}
          className={value === n.node_id ? 'node-tab active' : 'node-tab'}
          title={`last seen ${new Date(n.last_seen).toLocaleString()}`}
        >
          <span className={n.stale ? 'dot stale' : 'dot live'} />
          {editing === n.node_id ? (
            <input
              ref={inputRef}
              className="name-edit"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => void commitEdit()}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void commitEdit()
                else if (e.key === 'Escape') cancelEdit()
              }}
            />
          ) : (
            <>
              <button type="button" className="tab-label" onClick={() => onChange(n.node_id)}>
                {n.name}
              </button>
              <button
                type="button"
                className="edit-btn"
                title={`Rename ${n.name}`}
                aria-label={`Rename ${n.name}`}
                onClick={(e) => {
                  e.stopPropagation()
                  startEdit(n)
                }}
              >
                <PencilIcon />
              </button>
            </>
          )}
        </span>
      ))}

      {nodes.length === 0 && !error && <span className="muted small">no nodes seen yet</span>}
      {error && <span className="error small">/nodes: {error}</span>}
      {saveError && <span className="error small">{saveError}</span>}
    </div>
  )
}
