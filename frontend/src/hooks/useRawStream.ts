/**
 * useRawStream -- subscribe to the backend /live/raw WebSocket (raw packets).
 *
 * Separate from useWebSocket on purpose: different endpoint, different message
 * shape, and nothing here can affect the dashboard's /live connection.
 *
 *  - `ws(s)://<host>/live/raw[?node_id=N]`; reconnects with backoff (1s -> 15s).
 *  - Data messages have no `type`; typed messages are control (`ready`, `gap`)
 *    and are never rows.
 *  - Keeps the newest `bufferSize` packets (default 1000), newest LAST.
 *  - `paused` freezes what is returned without closing the socket; packets keep
 *    accumulating underneath and appear when resumed.
 */

import { useEffect, useRef, useState } from 'react'

export type RawPacket = {
  seq: number
  node_id: number | null
  seq_num: number | null
  ts: string
  crc_ok: boolean
  raw_hex: string
}

export type RawStatus = 'connecting' | 'open' | 'closed'

export function useRawStream(
  nodeId: number | null,
  opts: { bufferSize?: number; paused?: boolean } = {},
) {
  const bufferSize = opts.bufferSize ?? 1000
  const paused = opts.paused ?? false
  const [status, setStatus] = useState<RawStatus>('connecting')
  const [packets, setPackets] = useState<RawPacket[]>([])
  const [dropped, setDropped] = useState(0)

  const buf = useRef<RawPacket[]>([])
  const pausedRef = useRef(paused)
  const retry = useRef(0)

  pausedRef.current = paused
  // Resuming: show everything that arrived while frozen.
  useEffect(() => {
    if (!paused) setPackets(buf.current.slice())
  }, [paused])

  const clear = () => {
    buf.current = []
    setPackets([])
  }

  useEffect(() => {
    // Per-run state (NOT refs): a shared flag lets a superseded socket's onclose
    // see the next run's `false` and reconnect, leaving duplicate live sockets
    // all feeding one buffer (duplicated rows) -- StrictMode and node switches.
    let disposed = false
    let timer: number | undefined
    let socket: WebSocket | null = null
    buf.current = []
    setPackets([])
    setDropped(0)
    retry.current = 0

    const connect = () => {
      if (disposed) return
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const qs = nodeId != null ? `?node_id=${nodeId}` : ''
      const ws = new WebSocket(`${proto}//${window.location.host}/live/raw${qs}`)
      socket = ws
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
        const ctrl = (data as { type?: string }).type
        if (ctrl !== undefined) {
          if (ctrl === 'gap') setDropped((d) => d + ((data as { dropped?: number }).dropped ?? 0))
          return
        }
        const pkt = data as RawPacket
        const next = buf.current.length >= bufferSize ? buf.current.slice(-(bufferSize - 1)) : buf.current
        next.push(pkt)
        buf.current = next
        if (!pausedRef.current) setPackets(next.slice())
      }

      ws.onerror = () => ws.close()
      ws.onclose = () => {
        if (disposed) return   // superseded run: don't touch the new run's status
        setStatus('closed')
        const delay = Math.min(1000 * 2 ** retry.current, 15000)
        retry.current += 1
        timer = window.setTimeout(connect, delay)
      }
    }

    connect()
    return () => {
      disposed = true
      window.clearTimeout(timer)
      socket?.close()
      socket = null
    }
  }, [nodeId, bufferSize])

  return { status, packets, dropped, clear }
}
