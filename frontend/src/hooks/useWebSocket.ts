/**
 * useWebSocket -- subscribe to the backend /live WebSocket.
 *
 *   const { status, messages, last } = useWebSocket(nodeId)
 *
 *  - Opens `ws(s)://<host>/live?node_id=<nodeId>`; omits the query param when
 *    nodeId is null (server then streams every node).
 *  - Reconnects with exponential backoff (1s -> 15s cap), reset on a clean open.
 *  - **Any message carrying a `type` field is a control message and is never a
 *    reading.** Data frames have no `type` (see backend/ws/manager.py). Unknown
 *    control types are ignored rather than charted -- treating one as a reading
 *    would push a junk point into the series.
 *  - Control types handled: `ready` (handshake ack), `ping` (liveness, ignored),
 *    `gap` (the server had to drop messages for this client). A gap marks the
 *    NEXT reading with `gapBefore`, so the chart can break the line instead of
 *    drawing straight across missing samples as though they were real data.
 *  - Keeps the last `bufferSize` messages (default 300) as `messages`, the newest
 *    as `last`, and the running count of dropped messages as `dropped`.
 *  - Tears the socket down and reconnects fresh when nodeId changes / on unmount.
 */

import { useEffect, useRef, useState } from 'react'

export type LiveMessage = {
  node_id: number
  seq_num: number
  ts: string
  values: {
    tof_mm?: number
    tof_out_of_range?: boolean
    accel_mss?: [number, number, number]
    gyro_rads?: [number, number, number]
  }
  /**
   * Client-side only (not sent by the server): messages were dropped between
   * the previous reading and this one, so the series is discontinuous here.
   */
  gapBefore?: boolean
}

export type WsStatus = 'connecting' | 'open' | 'closed'

type Options = { bufferSize?: number }

export function useWebSocket(nodeId: number | null, opts: Options = {}) {
  const bufferSize = opts.bufferSize ?? 300
  const [status, setStatus] = useState<WsStatus>('connecting')
  const [messages, setMessages] = useState<LiveMessage[]>([])
  const [last, setLast] = useState<LiveMessage | null>(null)
  const [dropped, setDropped] = useState(0)

  const retry = useRef(0)
  const timer = useRef<number | undefined>(undefined)
  const socket = useRef<WebSocket | null>(null)
  const disposed = useRef(false)
  // Set by a `gap` control message, consumed by the next reading that arrives.
  const gapPending = useRef(false)

  useEffect(() => {
    disposed.current = false
    setMessages([])
    setLast(null)
    setDropped(0)
    gapPending.current = false
    retry.current = 0

    const connect = () => {
      if (disposed.current) return
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const qs = nodeId != null ? `?node_id=${nodeId}` : ''
      const ws = new WebSocket(`${proto}//${window.location.host}/live${qs}`)
      socket.current = ws
      setStatus('connecting')

      ws.onopen = () => {
        retry.current = 0
        setStatus('open')
      }

      ws.onmessage = (ev) => {
        let data: unknown
        try {
          data = JSON.parse(ev.data as string)
        } catch {
          return
        }
        if (!data || typeof data !== 'object') return

        // Control messages carry a `type`; readings never do. Anything typed is
        // handled here and MUST NOT fall through to the series.
        const ctrl = (data as { type?: string }).type
        if (ctrl !== undefined) {
          if (ctrl === 'gap') {
            const n = (data as { dropped?: number }).dropped ?? 0
            gapPending.current = true
            setDropped((d) => d + n)
          }
          // 'ready' and 'ping' need no action; unknown types are ignored.
          return
        }

        const msg = data as LiveMessage
        if (gapPending.current) {
          msg.gapBefore = true
          gapPending.current = false
        }
        setLast(msg)
        setMessages((prev) => {
          const next = prev.length >= bufferSize ? prev.slice(prev.length - bufferSize + 1) : prev.slice()
          next.push(msg)
          return next
        })
      }

      ws.onerror = () => ws.close()

      ws.onclose = () => {
        setStatus('closed')
        if (disposed.current) return
        const delay = Math.min(1000 * 2 ** retry.current, 15000)
        retry.current += 1
        timer.current = window.setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      disposed.current = true
      window.clearTimeout(timer.current)
      socket.current?.close()
      socket.current = null
    }
  }, [nodeId, bufferSize])

  return { status, messages, last, dropped }
}
