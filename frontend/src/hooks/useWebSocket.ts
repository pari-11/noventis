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

type Options = {
  bufferSize?: number
  /**
   * Opt-in (LiveChart only). Once per mount/node, also fetch the node's latest
   * `bufferSize` stored readings (GET /readings) and merge them in front of the live
   * stream, so the chart keeps its history across tab switches and refreshes like the
   * History panel does. Off by default: every other caller behaves exactly as before.
   */
  seedFromHistory?: boolean
}

/** One row of GET /readings (full-resolution rows; `rollup` rows are never used). */
export type ReadingRow = {
  node_id: number
  seq_num: number
  timestamp: string
  tof_mm: number | null
  tof_out_of_range?: boolean | null
  accel_x: number | null
  accel_y: number | null
  accel_z: number | null
  gyro_x: number | null
  gyro_y: number | null
  gyro_z: number | null
  rollup?: boolean
}

/** A stored reading -> the shape /live sends (so the chart code needs no changes). */
export function readingToMessage(r: ReadingRow): LiveMessage {
  const values: LiveMessage['values'] = {}
  if (r.tof_mm != null) {
    values.tof_mm = r.tof_mm
    values.tof_out_of_range = !!r.tof_out_of_range
  }
  if (r.accel_x != null && r.accel_y != null && r.accel_z != null) {
    values.accel_mss = [r.accel_x, r.accel_y, r.accel_z]
  }
  if (r.gyro_x != null && r.gyro_y != null && r.gyro_z != null) {
    values.gyro_rads = [r.gyro_x, r.gyro_y, r.gyro_z]
  }
  return { node_id: r.node_id, seq_num: r.seq_num, ts: r.timestamp, values }
}

/** Put `seeded` (stored history) and `live` (already received) together: drop seeded
 *  rows that are already live, keep time order, cap to the newest `cap`. Returns
 *  `live` itself (same reference) when there is nothing new to add. */
export function mergeSeed(live: LiveMessage[], seeded: LiveMessage[], cap: number): LiveMessage[] {
  const have = new Set(live.map((m) => `${m.node_id}:${m.seq_num}`))
  const older = seeded.filter((m) => !have.has(`${m.node_id}:${m.seq_num}`))
  if (older.length === 0) return live
  const decorated = older.concat(live).map((m) => [Date.parse(m.ts), m] as const)
  decorated.sort((a, b) => a[0] - b[0]) // stable: equal timestamps keep seeded-first order
  const all = decorated.map((d) => d[1])
  return all.length > cap ? all.slice(all.length - cap) : all
}

export function useWebSocket(nodeId: number | null, opts: Options = {}) {
  const bufferSize = opts.bufferSize ?? 300
  const seedFromHistory = opts.seedFromHistory ?? false
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
  // Newest timestamp (epoch ms) covered by the seeded history. A live message at or
  // before it is a duplicate of a seeded row (it was in the DB snapshot AND still in
  // flight on the socket) and is dropped. -Infinity => seeding off: no effect at all.
  const seedMaxTs = useRef(-Infinity)

  useEffect(() => {
    disposed.current = false
    setMessages([])
    setLast(null)
    setDropped(0)
    gapPending.current = false
    retry.current = 0
    seedMaxTs.current = -Infinity

    // Best-effort, once per run: a failure just leaves the chart live-only as before.
    let seedCancelled = false
    let seedTimer: number | undefined
    if (seedFromHistory && nodeId != null) {
      fetch(`/readings?node_id=${nodeId}&limit=${bufferSize}`)
        .then((r) => (r.ok ? (r.json() as Promise<ReadingRow[]>) : []))
        .then((rows) => {
          if (seedCancelled) return
          const seeded = rows
            .filter((r) => !r.rollup)
            .reverse() // the API returns newest first
            .map(readingToMessage)
          if (seeded.length === 0) return
          seedMaxTs.current = Math.max(...seeded.map((m) => Date.parse(m.ts)))
          // The in-flight-duplicate race lasts about a second; expire the rule after
          // 5 s so a stepped-back PC clock can never make it swallow live readings.
          seedTimer = window.setTimeout(() => {
            seedMaxTs.current = -Infinity
          }, 5000)
          setMessages((prev) => mergeSeed(prev, seeded, bufferSize))
        })
        .catch(() => {
          /* live-only */
        })
    }

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
        if (Date.parse(msg.ts) <= seedMaxTs.current) return // duplicate of a seeded row
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
      seedCancelled = true
      window.clearTimeout(seedTimer)
      disposed.current = true
      window.clearTimeout(timer.current)
      socket.current?.close()
      socket.current = null
    }
  }, [nodeId, bufferSize, seedFromHistory])

  return { status, messages, last, dropped }
}
