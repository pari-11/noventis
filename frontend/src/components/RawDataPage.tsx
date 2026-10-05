/**
 * RawDataPage -- "View Raw data": every packet exactly as it came off the radio.
 *
 * Streams /live/raw (see hooks/useRawStream) and renders each frame's hex with
 * the fields colour-split:  SYNC | NODE | SEQ | LEN | PAYLOAD (TLVs) | CRC.
 * Layout per docs/protocol-spec.md section 1. Nothing here decodes values; the
 * only interpretation is slicing the bytes by their documented offsets.
 *
 * Filters: node, CRC (all / ok / failed), hex substring search. Plus pause,
 * clear, and copy. Rows are newest-first, capped to what the hook buffers.
 */

import { useEffect, useMemo, useState } from 'react'
import { useRawStream } from '../hooks/useRawStream'
import type { NodeInfo } from './NodeSelector'

type Seg = { cls: string; text: string; title: string }

const TAGS: Record<string, string> = { '01': 'ToF (tag 0x01)', '02': 'IMU 6-axis (tag 0x02)' }

/** Slice a frame's hex into labelled segments. Malformed frames fall back to one grey chunk. */
function segment(hex: string): Seg[] {
  const bytes = hex.match(/../g) ?? []
  if (bytes.length < 8) return [{ cls: 'raw-bad', text: bytes.join(' '), title: 'too short for a frame' }]
  const len = parseInt(bytes[5], 16)
  const segs: Seg[] = [
    { cls: 'raw-sync', text: bytes.slice(0, 2).join(' '), title: 'SYNC' },
    { cls: 'raw-node', text: bytes[2], title: 'NODE_ID' },
    { cls: 'raw-seq', text: bytes.slice(3, 5).join(' '), title: 'SEQ_NUM (big-endian)' },
    { cls: 'raw-len', text: bytes[5], title: `PAYLOAD_LEN = ${len}` },
  ]
  const end = 6 + len
  if (bytes.length !== end + 2) {
    // length byte disagrees with what arrived (e.g. corruption) -- don't pretend to parse it
    segs.push({ cls: 'raw-bad', text: bytes.slice(6).join(' '), title: 'length mismatch: payload/CRC boundary unknown' })
    return segs
  }
  let i = 6
  while (i < end) {
    const tag = bytes[i]
    const l = i + 1 < end ? parseInt(bytes[i + 1], 16) : 0
    const stop = Math.min(i + 2 + l, end)
    segs.push({ cls: 'raw-tlv', text: bytes.slice(i, stop).join(' '), title: TAGS[tag] ?? `unknown tag 0x${tag}` })
    i = stop
  }
  segs.push({ cls: 'raw-crc', text: bytes.slice(end).join(' '), title: 'CRC-16/CCITT-FALSE' })
  return segs
}

function timeOf(ts: string): string {
  const d = new Date(ts)
  return `${d.toLocaleTimeString([], { hour12: false })}.${String(d.getMilliseconds()).padStart(3, '0')}`
}

const spacedHex = (hex: string) => (hex.match(/../g) ?? []).join(' ')

type CrcFilter = 'all' | 'ok' | 'fail'

export function RawDataPage({ initialNodeId }: { initialNodeId: number | null }) {
  const [nodeId, setNodeId] = useState<number | null>(initialNodeId)
  const [nodes, setNodes] = useState<NodeInfo[]>([])
  const [crc, setCrc] = useState<CrcFilter>('all')
  const [query, setQuery] = useState('')
  const [paused, setPaused] = useState(false)
  const { status, packets, dropped, clear } = useRawStream(nodeId, { paused })

  useEffect(() => {
    let alive = true
    fetch('/nodes')
      .then((r) => (r.ok ? (r.json() as Promise<NodeInfo[]>) : []))
      .then((d) => alive && setNodes(d))
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  // Everything matching the filters, oldest -> newest. The table shows only the
  // newest 300 of these; CSV export takes all of them.
  const matching = useMemo(() => {
    const q = query.replace(/[\s:]/g, '').toLowerCase()
    return packets.filter(
      (p) =>
        !(crc === 'ok' && !p.crc_ok) &&
        !(crc === 'fail' && p.crc_ok) &&
        (!q || p.raw_hex.includes(q)),
    )
  }, [packets, crc, query])
  const rows = useMemo(() => matching.slice(-300).reverse(), [matching])

  const downloadCsv = () => {
    // Hex is space-separated so spreadsheets keep it as text (a bare "1e5" would
    // be read as a number). Oldest first; times are the backend's receive time.
    const lines = ['received_at,node_id,seq_num,crc_ok,packet_hex']
    for (const p of matching) {
      lines.push([p.ts, p.node_id ?? '', p.seq_num ?? '', p.crc_ok ? 'OK' : 'FAIL', `"${spacedHex(p.raw_hex)}"`].join(','))
    }
    const url = URL.createObjectURL(new Blob([lines.join('\r\n') + '\r\n'], { type: 'text/csv;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `noventis-raw-${new Date().toISOString().replace(/[:.]/g, '-')}.csv`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  const copy = (text: string) => void navigator.clipboard?.writeText(text).catch(() => {})
  const failed = useMemo(() => packets.filter((p) => !p.crc_ok).length, [packets])

  return (
    <main className="raw-page">
      <div className="raw-head">
        <h2 className="section-title" style={{ margin: 0 }}>Raw data</h2>
        <span className={`ws-badge ${status}`}>{status === 'open' ? 'streaming' : status}</span>
        <span className="muted small">
          {packets.length} buffered · {failed} CRC fail{dropped > 0 ? ` · ${dropped} missed (slow client)` : ''}
        </span>
      </div>

      <div className="raw-filters">
        <label>
          Node{' '}
          <select value={nodeId ?? ''} onChange={(e) => setNodeId(e.target.value === '' ? null : Number(e.target.value))}>
            <option value="">All nodes</option>
            {nodes.map((n) => (
              <option key={n.node_id} value={n.node_id}>{n.name ?? `Node ${n.node_id}`}</option>
            ))}
          </select>
        </label>
        <label>
          CRC{' '}
          <select value={crc} onChange={(e) => setCrc(e.target.value as CrcFilter)}>
            <option value="all">All</option>
            <option value="ok">OK only</option>
            <option value="fail">Failed only</option>
          </select>
        </label>
        <input
          className="raw-search"
          placeholder="Search hex (e.g. aa55 or 01 02)"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button className="btn" onClick={() => setPaused((p) => !p)}>{paused ? 'Resume' : 'Pause'}</button>
        <button className="btn" onClick={clear}>Clear</button>
        <button className="btn" onClick={() => copy(rows.map((p) => spacedHex(p.raw_hex)).join('\n'))} disabled={rows.length === 0}>
          Copy shown
        </button>
        <button className="btn" onClick={downloadCsv} disabled={matching.length === 0} title="Download the filtered packets as CSV">
          Download CSV
        </button>
      </div>

      <div className="raw-legend small">
        <span className="raw-sync">SYNC</span> <span className="raw-node">NODE</span>{' '}
        <span className="raw-seq">SEQ</span> <span className="raw-len">LEN</span>{' '}
        <span className="raw-tlv">PAYLOAD (TLVs)</span> <span className="raw-crc">CRC</span>{' '}
        <span className="raw-bad">unparseable</span>
      </div>

      {rows.length === 0 ? (
        <p className="muted small">{packets.length === 0 ? 'waiting for packets…' : 'no packets match the filters'}</p>
      ) : (
        <table className="raw-table">
          <thead>
            <tr><th>Time</th><th>Node</th><th>Seq</th><th>CRC</th><th>Packet (hex)</th><th /></tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.seq} className={p.crc_ok ? '' : 'raw-row-fail'}>
                <td>{timeOf(p.ts)}</td>
                <td>{p.node_id ?? '—'}</td>
                <td>{p.seq_num ?? '—'}</td>
                <td>{p.crc_ok ? 'OK' : 'FAIL'}</td>
                <td className="raw-hex">
                  {segment(p.raw_hex).map((s, i) => (
                    <span key={i} className={s.cls} title={s.title}>{s.text} </span>
                  ))}
                </td>
                <td><button className="btn ghost-btn" onClick={() => copy(spacedHex(p.raw_hex))} title="Copy this packet">copy</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  )
}
