/**
 * useWebSocket -- subscribe to the backend /live WebSocket.
 *
 *   const { lastMessage, status } = useWebSocket(nodeId)
 *
 *  - Opens `ws://<host>/live?node_id=<nodeId>`; omits the query param when
 *    nodeId is null (server then streams all nodes).
 *  - Reconnects with exponential backoff on close/error.
 *  - Parses each JSON message (shape per docs/protocol-spec.md section 3:
 *    { node_id, seq_num, ts, values }).
 *  - Tears down and reconnects when nodeId changes or the component unmounts.
 *
 * TODO:
 *  - [ ] connection lifecycle in useEffect keyed on nodeId
 *  - [ ] exponential backoff (cap ~10s) + jitter
 *  - [ ] expose connection status: 'idle' | 'connecting' | 'open' | 'closed'
 *  - [ ] typed LiveMessage model shared with LiveChart
 *  - [ ] optional ring buffer of the last N messages instead of just the last
 */

// values keys per docs/protocol-spec.md section 3:
//   tof_mm: number | gyro_rads: [x,y,z] | accel_mss: [x,y,z]
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

export type WsStatus = 'idle' | 'connecting' | 'open' | 'closed'

export function useWebSocket(_nodeId: number | null): {
  lastMessage: LiveMessage | null
  status: WsStatus
} {
  // TODO: implement -- see docstring.
  return { lastMessage: null, status: 'idle' }
}
