/**
 * LiveChart -- real-time plot of incoming telemetry for the selected node(s).
 *
 * Consumes useWebSocket(nodeId) and renders three dependency-free SVG sparklines
 * over a rolling window: ToF distance (mm), |acceleration| (m/s^2), |angular
 * rate| (rad/s). When nodeId is null the series interleave every node's frames.
 *
 * Swap the <Sparkline> for recharts/uPlot later if richer axes are needed.
 */

import { useMemo } from 'react'
import { useWebSocket } from '../hooks/useWebSocket'

function magnitude(v?: [number, number, number]): number | undefined {
  if (!v) return undefined
  return Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
}

function Sparkline({
  data,
  color,
  unit,
  label,
  decimals,
}: {
  data: number[]
  color: string
  unit: string
  label: string
  decimals: number
}) {
  const W = 480
  const H = 88
  const PAD = 4
  const pts = data.filter((x) => Number.isFinite(x))
  const min = pts.length ? Math.min(...pts) : 0
  const max = pts.length ? Math.max(...pts) : 1
  const span = max - min || 1

  const path = pts
    .map((y, i) => {
      const px = PAD + (i / Math.max(pts.length - 1, 1)) * (W - 2 * PAD)
      const py = H - PAD - ((y - min) / span) * (H - 2 * PAD)
      return `${i === 0 ? 'M' : 'L'}${px.toFixed(1)},${py.toFixed(1)}`
    })
    .join(' ')

  const latest = pts.length ? pts[pts.length - 1] : undefined

  return (
    <div className="spark">
      <div className="spark-head">
        <span className="spark-label">{label}</span>
        <span className="spark-value" style={{ color }}>
          {latest != null ? latest.toFixed(decimals) : '--'} {unit}
        </span>
      </div>
      <svg className="spark-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        {pts.length > 1 && <path d={path} fill="none" stroke={color} strokeWidth={1.5} />}
      </svg>
      <div className="spark-foot muted">
        <span>{pts.length ? min.toFixed(decimals) : ''}</span>
        <span>{pts.length} pts</span>
        <span>{pts.length ? max.toFixed(decimals) : ''}</span>
      </div>
    </div>
  )
}

type Props = { nodeId: number | null }

export function LiveChart({ nodeId }: Props) {
  const { status, messages, last } = useWebSocket(nodeId)

  const series = useMemo(() => {
    const tof: number[] = []
    const acc: number[] = []
    const gyr: number[] = []
    for (const m of messages) {
      if (m.values.tof_mm != null) tof.push(m.values.tof_mm)
      const a = magnitude(m.values.accel_mss)
      if (a != null) acc.push(a)
      const g = magnitude(m.values.gyro_rads)
      if (g != null) gyr.push(g)
    }
    return { tof, acc, gyr }
  }, [messages])

  return (
    <section className="card live-chart">
      <header className="card-head">
        <h2>Live {nodeId == null ? '· all nodes' : `· node ${nodeId}`}</h2>
        <span className={`ws-badge ${status}`}>{status}</span>
      </header>

      {last ? (
        <p className="muted small">
          latest: node {last.node_id} · seq {last.seq_num} · {new Date(last.ts).toLocaleTimeString()}
        </p>
      ) : (
        <p className="muted small">waiting for frames…</p>
      )}

      <Sparkline data={series.tof} color="#2563eb" unit="mm" label="ToF distance" decimals={0} />
      <Sparkline data={series.acc} color="#16a34a" unit="m/s²" label="|acceleration|" decimals={2} />
      <Sparkline data={series.gyr} color="#d97706" unit="rad/s" label="|angular rate|" decimals={3} />
    </section>
  )
}
