/**
 * useWebSocket -- subscribe to the backend /live WebSocket.
 *
 *   const { status, messages, last } = useWebSocket(nodeId)
 *
 *  - Opens `ws(s)://<host>/live?node_id=<nodeId>`; omits the query param when
 *    nodeId is null (server then streams every node).
 *  - Reconnects with exponential backoff (1s -> 15s cap), reset on a clean open.
 *  - Ignores the `{type:"ready"}` handshake ack; every other message is a
 *    LiveMessage. Keeps the last `bufferSize` messages (default 300) as `messages`
 *    and the newest as `last`.
 *  - Tears the socket down and reconnects fresh when nodeId changes / on unmount.
 */

import { useEffect, useRef, useState } from 'react'

export type LiveMessage = {
  node_id: number
  seq_num: number
  ts: string
  values: {
    tof_mm?: number
    accel_mss?: [number, number, number]
    gyro_rads?: [number, number, number]
  }
}

export type WsStatus = 'connecting' | 'open' | 'closed'

type Options = { bufferSize?: number }

export function useWebSocket(nodeId: number | null, opts: Options = {}) {
  const bufferSize = opts.bufferSize ?? 300
  const [status, setStatus] = useState<WsStatus>('connecting')
  const [messages, setMessages] = useState<LiveMessage[]>([])
  const [last, setLast] = useState<LiveMessage | null>(null)

  const retry = useRef(0)
  const timer = useRef<number | undefined>(undefined)
  const socket = useRef<WebSocket | null>(null)
  const disposed = useRef(false)

  useEffect(() => {
    disposed.current = false
    setMessages([])
    setLast(null)
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
        if ((data as { type?: string }).type === 'ready') return

        const msg = data as LiveMessage
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

  return { status, messages, last }
}
