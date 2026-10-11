/**
 * useRawStream -- subscribe to the backend /live/raw WebSocket (raw packets).
 *
 * Separate from useWebSocket on purpose: different endpoint, different message
 * shape, and nothing here can affect the dashboard's /live connection.
 *
 *  - `ws(s)://<host>/live/raw[?node_id=N]`; reconnects with backoff (1s -> 15s).
 *  - Data messages have no `type`; typed messages are control (`ready`, `gap`)
 *    and are never rows.
 *  - Keeps the newest `bufferSize` packets (default 5000), newest LAST.
 *  - `paused` freezes what is returned without closing the socket; packets keep
 *    accumulating underneath and appear when resumed.
 *  - PRELOAD: on every (re)connect it also fetches the backend's in-memory ring of
 *    recent frames (GET /debug/frames, the last RAW_FRAME_BUFFER_SIZE = 5000) and
 *    merges it in, so opening the page for the first time or refreshing it shows
 *    the packets since the backend started instead of only those from the moment
 *    the page opened -- like the readings history. It is cleared only when the
 *    backend restarts (the ring is in memory). Preloaded and live packets are
 *    de-duplicated by (timestamp, bytes). A failed preload is ignored: the page
 *    simply behaves as it did before (live only). "Clear" stays cleared.
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

/** One row of GET /debug/frames (same bytes as /live/raw, different field names). */
export type DebugFrame = {
  seq: number
  node_id: number | null
  received_at: string
  crc_ok: boolean
  raw_hex: string
}

/** Identity of a packet across the two sources. /live/raw sends `ts` as
 *  "...+00:00" and /debug/frames as "...Z"; parsing to epoch ms makes them equal. */
export const keyOf = (p: RawPacket): string => `${Date.parse(p.ts)}|${p.raw_hex}`

/** /debug/frames row -> the shape /live/raw sends. SEQ_NUM is header bytes 3-4
 *  (hex chars 6..10) and is only meaningful when the header parsed (node_id set). */
export function toPacket(f: DebugFrame): RawPacket {
  const hex = f.raw_hex
  const seqNum = f.node_id != null && hex.length >= 10 ? parseInt(hex.slice(6, 10), 16) : null
  return { seq: f.seq, node_id: f.node_id, seq_num: seqNum, ts: f.received_at, crc_ok: f.crc_ok, raw_hex: hex }
}

/** Add `incoming` packets not already in `current`, keep time order, cap to the
 *  newest `cap`. Returns `current` itself (same reference) when nothing is new. */
export function mergePackets(current: RawPacket[], incoming: RawPacket[], cap: number): RawPacket[] {
  const seen = new Set(current.map(keyOf))
  const fresh: RawPacket[] = []
  for (const p of incoming) {
    const k = keyOf(p)
    if (!seen.has(k)) {
      seen.add(k)
      fresh.push(p)
    }
  }
  if (fresh.length === 0) return current
  const decorated = current.concat(fresh).map((p) => [Date.parse(p.ts), p] as const)
  // Time order; packets in the same millisecond (a burst after a stall) are ordered
  // by their arrival counter `seq`, so the result never depends on input order.
  decorated.sort((a, b) => a[0] - b[0] || a[1].seq - b[1].seq)
  const all = decorated.map((d) => d[1])
  return all.length > cap ? all.slice(all.length - cap) : all
}

export function useRawStream(
  nodeId: number | null,
  opts: { bufferSize?: number; paused?: boolean } = {},
) {
  const bufferSize = opts.bufferSize ?? 5000
  const paused = opts.paused ?? false
  const [status, setStatus] = useState<RawStatus>('connecting')
  const [packets, setPackets] = useState<RawPacket[]>([])
  const [dropped, setDropped] = useState(0)

  const buf = useRef<RawPacket[]>([])
  const keys = useRef<Set<string>>(new Set()) // keyOf() of everything in `buf`
  const clearedAfter = useRef(0) // epoch ms: preload ignores packets at/before this
  const pausedRef = useRef(paused)
  const retry = useRef(0)

  pausedRef.current = paused
  // Resuming: show everything that arrived while frozen.
  useEffect(() => {
    if (!paused) setPackets(buf.current.slice())
  }, [paused])

  const clear = () => {
    // Remember where we cleared (by the newest packet's own timestamp, so no
    // browser/backend clock difference matters) so a reconnect's preload cannot
    // bring the cleared rows back.
    const last = buf.current[buf.current.length - 1]
    clearedAfter.current = last ? Date.parse(last.ts) : Date.now()
    buf.current = []
    keys.current = new Set()
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
    keys.current = new Set()
    clearedAfter.current = 0
    setPackets([])
    setDropped(0)
    retry.current = 0

    // Fetch the backend's recent frames and merge them in (see header comment).
    const preload = async () => {
      try {
        const qs = `limit=${bufferSize}` + (nodeId != null ? `&node_id=${nodeId}` : '')
        const res = await fetch(`/debug/frames?${qs}`)
        if (!res.ok) return
        const frames = (await res.json()) as DebugFrame[]
        if (disposed) return
        const incoming = frames.map(toPacket).filter((p) => Date.parse(p.ts) > clearedAfter.current)
        const merged = mergePackets(buf.current, incoming, bufferSize)
        if (merged === buf.current) return
        buf.current = merged
        keys.current = new Set(merged.map(keyOf))
        if (!pausedRef.current) setPackets(merged.slice())
      } catch {
        /* preload is best-effort: carry on with live packets only */
      }
    }

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
        void preload() // also fills the gap after a reconnect
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
        const k = keyOf(pkt)
        if (keys.current.has(k)) return // already there from the preload
        keys.current.add(k)
        const arr = buf.current
        arr.push(pkt)
        if (arr.length > bufferSize) {
          for (const old of arr.splice(0, arr.length - bufferSize)) keys.current.delete(keyOf(old))
        }
        if (!pausedRef.current) setPackets(arr.slice())
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
