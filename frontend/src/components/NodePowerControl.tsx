/**
 * NodePowerControl -- "Shut down node" for the selected node.
 *
 * POSTs /nodes/{id}/shutdown, which sends an authenticated command over LoRa and
 * WAITS (up to ~10 s) for the real outcome -- the same click -> spinner -> result
 * pattern as the header's "Rescan for nodes". No WebSocket involved.
 *
 * Outcomes (see backend/control/shutdown.py):
 *   acked         green  -- the node confirmed and is powering off
 *   silent        amber  -- no confirmation, but it stopped transmitting (probably off)
 *   not_received  red    -- it is still transmitting: the command did not get through
 * Once a node is off, only physically replugging power starts it again. Pressing
 * OK on an acked/silent result calls `onPoweredOff`, so the dashboard can swap the
 * node's graphs for an "unreachable, please replug" card (App.tsx clears it again
 * when the node transmits).
 */

import { useEffect, useRef, useState } from 'react'

type Outcome = 'acked' | 'silent' | 'not_received'
type State =
  | { kind: 'idle' }
  | { kind: 'confirm' }
  | { kind: 'sending' }
  | { kind: 'done'; outcome: Outcome; message: string }
  | { kind: 'error'; message: string }

export function NodePowerControl({
  nodeId,
  nodeName,
  onPoweredOff,
}: {
  nodeId: number
  nodeName?: string
  onPoweredOff: (nodeId: number) => void
}) {
  const [state, setState] = useState<State>({ kind: 'idle' })
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const label = nodeName ?? `Node ${nodeId}`

  const send = async () => {
    setState({ kind: 'sending' })
    try {
      const res = await fetch(`/nodes/${nodeId}/shutdown`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true }),
      })
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try {
          const body = (await res.json()) as { detail?: string }
          if (typeof body.detail === 'string') detail = body.detail
        } catch {
          /* keep the HTTP status */
        }
        throw new Error(detail)
      }
      const data = (await res.json()) as { outcome: Outcome; message: string }
      if (alive.current) setState({ kind: 'done', outcome: data.outcome, message: data.message })
    } catch (e) {
      if (alive.current) setState({ kind: 'error', message: e instanceof Error ? e.message : String(e) })
    }
  }

  return (
    <div className="node-power">
      {state.kind === 'idle' && (
        <button className="btn" onClick={() => setState({ kind: 'confirm' })} title="Safely power off this node over LoRa">
          Shut down node
        </button>
      )}

      {state.kind === 'confirm' && (
        <>
          <span className="small">
            Shut down <strong>{label}</strong>? It stops until power is unplugged and replugged.
          </span>
          <button className="btn accent" onClick={() => void send()}>Yes, shut down</button>
          <button className="btn ghost-btn" onClick={() => setState({ kind: 'idle' })}>Cancel</button>
        </>
      )}

      {state.kind === 'sending' && (
        <>
          <span className="spinner" role="status" aria-label="Shutting down" />
          <span className="small muted">Sending shutdown command to {label}… (up to 10 s)</span>
        </>
      )}

      {state.kind === 'done' && (
        <>
          <span className={`small power-${state.outcome}`}>{state.message}</span>
          <button
            className="btn"
            onClick={() => {
              if (state.outcome === 'acked' || state.outcome === 'silent') onPoweredOff(nodeId)
              setState({ kind: 'idle' })
            }}
          >
            OK
          </button>
        </>
      )}

      {state.kind === 'error' && (
        <>
          <span className="small power-not_received">{state.message}</span>
          <button className="btn ghost-btn" onClick={() => setState({ kind: 'idle' })}>OK</button>
        </>
      )}
    </div>
  )
}
