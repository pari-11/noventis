/**
 * LiveChart -- one node's live telemetry.
 *
 * DEFAULT VIEW is a compact readout: the current value (raw or smoothed, per the
 * toggle) of every active sensor -- ToF (mm), |acceleration| (m/s^2),
 * |angular rate| (rad/s) -- as plain stat tiles, no chart rendered.
 *
 * "View chart" expands a zoomable lightweight-charts (v4) plot of ONE series at a
 * time (ToF / |accel| / |gyro|, chosen with the in-chart selector). The three
 * quantities live on wildly different scales (ToF in hundreds of mm, accel in
 * single-digit m/s^2, gyro in fractional rad/s) and do not share a y-axis
 * meaningfully, so only the selected series is drawn. A time-range selector
 * (1 min / 5 min / All) trims what is charted. Collapsing back to the compact
 * view keeps the raw/smoothed, series and time-range selections for next expand
 * (they are React state on this always-mounted component); only the chart's
 * pan/zoom is rebuilt.
 *
 * Time axis: lightweight-charts renders numeric times in UTC by default, which
 * disagreed visibly with the "latest:" text (browser local time). Both the axis
 * tick labels (timeScale.tickMarkFormatter) and the crosshair tooltip
 * (localization.timeFormatter) now format via Intl.DateTimeFormat with no
 * explicit timeZone -> the browser's local zone, matching "latest:".
 *
 * ToF handling (see backend TOF_MAX_VALID_MM): the VL53L0X "no target" sentinel
 * (~8190 mm) arrives flagged `tof_out_of_range`. Those points are NOT fed into
 * the ToF line as a distance -- the line breaks (whitespace gap) and a red dot
 * is drawn instead, so one bogus reading can't blow out the y-axis.
 *
 * Raw / Smoothed toggle: "Smoothed" is display-only. It applies a trailing
 * rolling median (window 11) followed by a short trailing moving average
 * (window 5) to whichever series is shown / read out, client-side, over the
 * points already in the buffer. It never changes how many points are shown, what
 * is charted's cadence, or what is stored. "Raw" shows completely unfiltered
 * values.
 *
 * Scroll-to-zoom / drag-to-pan are the library defaults and are left enabled.
 * We fitContent() on the first batch (and on a series / range switch); after that
 * we follow the live edge only while the user hasn't grabbed the chart.
 * "Grabbed" is detected from real wheel / pointer / touch events on the canvas --
 * NOT from subscribeVisibleLogicalRangeChange, which also fires for our own
 * setData/fitContent/scrollToRealTime. Mounted per-node via a `key={nodeId}` in
 * App, so switching nodes recreates the component and its view.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import {
  createChart,
  ColorType,
  type AutoscaleInfo,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  type LogicalRange,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts'
import { useWebSocket, type LiveMessage } from '../hooks/useWebSocket'

const CHART_HEIGHT = 320
const MEDIAN_WINDOW = 11 // was 5 -- too small, noise still showed through (issue #3)
const MA_WINDOW = 5 // light second pass on top of the median
const INPUT_GRACE_MS = 600 // window after a real gesture in which range changes count as "user"
const WS_BUFFER = 600 // ~5 min of frames at 2 Hz, so the range selector has history to trim

type Mode = 'raw' | 'smoothed'
type SeriesKey = 'tof' | 'accel' | 'gyro'

// `minSpan` is the smallest y-axis window we let the price scale autoscale to.
// Without it a near-constant trace (e.g. ToF holding at ~101 mm) gets a ~2 mm
// axis and every integer-mm step looks like violent noise; this keeps the
// default view zoomed out and readable.
const SERIES_META: Record<
  SeriesKey,
  { label: string; unit: string; color: string; minSpan: number }
> = {
  tof: { label: 'ToF', unit: 'mm', color: '#2563eb', minSpan: 20 },
  accel: { label: '|accel|', unit: 'm/s²', color: '#16a34a', minSpan: 2 },
  gyro: { label: '|gyro|', unit: 'rad/s', color: '#d97706', minSpan: 1 },
}
const SERIES_KEYS = Object.keys(SERIES_META) as SeriesKey[]

const RANGES: { label: string; ms: number }[] = [
  { label: '1 min', ms: 60_000 },
  { label: '5 min', ms: 300_000 },
  { label: 'All', ms: Infinity },
]

// Local-time formatters for the chart. No `timeZone` option -> the browser's
// local zone, the same zone `Date#toLocaleTimeString()` uses for the "latest:"
// text, so the axis / crosshair and that text always agree (issue #1).
const fmtHMS = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})
const fmtHM = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})
/** epoch-seconds (our chart time) -> local wall-clock string */
const localHMS = (t: number) => fmtHMS.format(new Date(t * 1000))

function magnitude(v?: [number, number, number]): number | undefined {
  if (!v) return undefined
  return Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
}

/** Trailing rolling median: out[i] = median of values[max(0, i-win+1) .. i]. */
function rollingMedian(values: number[], win: number): number[] {
  return values.map((_, i) => {
    const w = values.slice(Math.max(0, i - win + 1), i + 1).sort((a, b) => a - b)
    return w[Math.floor(w.length / 2)]
  })
}

/** Trailing simple moving average over the same kind of window. */
function movingAverage(values: number[], win: number): number[] {
  return values.map((_, i) => {
    const w = values.slice(Math.max(0, i - win + 1), i + 1)
    return w.reduce((s, x) => s + x, 0) / w.length
  })
}

/** Display-only smoothing: rolling median, then a short moving-average pass. */
function smooth(values: number[]): number[] {
  return movingAverage(rollingMedian(values, MEDIAN_WINDOW), MA_WINDOW)
}

function fmtValue(key: SeriesKey, v: number): string {
  if (key === 'tof') return Math.round(v).toString()
  return Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(3)
}

/** One row per frame on a strictly-increasing timeline (2 Hz frames stay unique). */
type Frame = { time: UTCTimestamp; tof?: number; tofOor?: boolean; accel?: number; gyro?: number }

function toFrames(messages: LiveMessage[]): Frame[] {
  const out: Frame[] = []
  let lastT = -Infinity
  for (const m of messages) {
    let t = Date.parse(m.ts) / 1000
    if (!(t > lastT)) t = lastT + 0.001
    lastT = t
    const f: Frame = { time: t as UTCTimestamp }
    if (m.values.tof_mm != null) {
      f.tof = m.values.tof_mm
      f.tofOor = !!m.values.tof_out_of_range
    }
    const a = magnitude(m.values.accel_mss)
    if (a != null) f.accel = a
    const g = magnitude(m.values.gyro_rads)
    if (g != null) f.gyro = g
    out.push(f)
  }
  return out
}

/** in-range numeric samples for a series (ToF drops out-of-range sentinels). */
function samplesFor(frames: Frame[], key: SeriesKey): { time: UTCTimestamp; value: number }[] {
  const out: { time: UTCTimestamp; value: number }[] = []
  for (const f of frames) {
    if (key === 'tof') {
      if (f.tof != null && !f.tofOor) out.push({ time: f.time, value: f.tof })
    } else if (f[key] != null) {
      out.push({ time: f.time, value: f[key] as number })
    }
  }
  return out
}

type Props = { nodeId: number }

export function LiveChart({ nodeId }: Props) {
  const { status, messages, last } = useWebSocket(nodeId, { bufferSize: WS_BUFFER })

  // all four selections are plain state on this always-mounted component, so
  // collapsing the chart never loses them (issue #2).
  const [mode, setMode] = useState<Mode>('raw')
  const [expanded, setExpanded] = useState(false)
  const [series, setSeries] = useState<SeriesKey>('tof')
  const [rangeMs, setRangeMs] = useState<number>(RANGES[1].ms)

  const frames = useMemo(() => toFrames(messages), [messages])

  // compact readout: current (last) value per sensor, smoothed or raw per toggle
  const current = useMemo(() => {
    const pick = (key: SeriesKey): number | undefined => {
      const pts = samplesFor(frames, key).map((p) => p.value)
      if (pts.length === 0) return undefined
      const s = mode === 'smoothed' ? smooth(pts) : pts
      return s[s.length - 1]
    }
    let tofOor = false
    for (let i = frames.length - 1; i >= 0; i--) {
      if (frames[i].tof != null) {
        tofOor = !!frames[i].tofOor
        break
      }
    }
    return { tof: pick('tof'), accel: pick('accel'), gyro: pick('gyro'), tofOor }
  }, [frames, mode])

  const available = useMemo<Record<SeriesKey, boolean>>(
    () => ({
      tof: current.tof != null || current.tofOor,
      accel: current.accel != null,
      gyro: current.gyro != null,
    }),
    [current],
  )

  // if the charted series goes away (e.g. only ToF is arriving), fall back
  useEffect(() => {
    if (!available[series]) {
      const first = SERIES_KEYS.find((k) => available[k])
      if (first) setSeries(first)
    }
  }, [available, series])

  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const lineRef = useRef<ISeriesApi<'Line'> | null>(null)
  const oorRef = useRef<ISeriesApi<'Line'> | null>(null)

  const pointCount = useRef(0)
  const minSpanRef = useRef(SERIES_META.tof.minSpan) // current series' min y-axis span
  const atLiveEdge = useRef(true)
  const didInitialFit = useRef(false)
  const lastInputAt = useRef(0) // performance.now() of the last real wheel/pointer/touch
  const [showBackToLive, setShowBackToLive] = useState(false)

  // create / tear down the chart with the expanded view
  useEffect(() => {
    if (!expanded) return
    const el = containerRef.current
    if (!el) return

    const chart = createChart(el, {
      width: el.clientWidth,
      height: CHART_HEIGHT,
      layout: {
        background: { type: ColorType.Solid, color: '#ffffff' },
        textColor: '#6b7280',
        fontSize: 11,
        attributionLogo: false, // drop the TradingView watermark (v4.2+)
      },
      grid: {
        vertLines: { color: '#f0f1f4' },
        horzLines: { color: '#f0f1f4' },
      },
      rightPriceScale: { borderColor: '#e2e5ea', scaleMargins: { top: 0.15, bottom: 0.15 } },
      timeScale: {
        borderColor: '#e2e5ea',
        timeVisible: true,
        secondsVisible: true,
        // axis tick labels in the browser's local zone (issue #1)
        tickMarkFormatter: (time: unknown, tickMarkType: number) => {
          const d = new Date(Number(time) * 1000)
          return tickMarkType >= 4 ? fmtHMS.format(d) : fmtHM.format(d)
        },
      },
      localization: {
        // crosshair tooltip time in the browser's local zone (issue #1)
        timeFormatter: (time: unknown) => localHMS(Number(time)),
      },
    })
    chartRef.current = chart

    lineRef.current = chart.addLineSeries({
      color: SERIES_META[series].color,
      lineWidth: 2,
      priceScaleId: 'right',
      // widen the autoscale to at least the current series' minSpan so a steady
      // trace doesn't get magnified into noise (see SERIES_META.minSpan)
      autoscaleInfoProvider: (base: () => AutoscaleInfo | null) => {
        const r = base()
        if (!r || !r.priceRange) return r
        const { minValue, maxValue } = r.priceRange
        const need = minSpanRef.current
        if (maxValue - minValue >= need) return r
        const mid = (minValue + maxValue) / 2
        return { ...r, priceRange: { minValue: mid - need / 2, maxValue: mid + need / 2 } }
      },
    })
    // ToF out-of-range markers: dots only, pinned to the price scale, excluded
    // from its autoscale so they never widen the axis. Empty for accel/gyro.
    oorRef.current = chart.addLineSeries({
      color: '#dc2626',
      lineWidth: 1,
      lineVisible: false,
      pointMarkersVisible: true,
      pointMarkersRadius: 3,
      priceScaleId: 'right',
      lastValueVisible: false,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
      autoscaleInfoProvider: () => null,
    })

    // A real gesture on the canvas -- the ONLY thing that may detach the view
    // from the live edge. Programmatic view changes never touch this ref.
    const markInput = () => {
      lastInputAt.current = performance.now()
    }
    el.addEventListener('wheel', markInput, { passive: true })
    el.addEventListener('pointerdown', markInput)
    el.addEventListener('touchstart', markInput, { passive: true })

    const onRange = (range: LogicalRange | null) => {
      if (!range || pointCount.current === 0) return
      // ignore range changes that weren't driven by a recent user gesture
      // (setData / fitContent / scrollToRealTime / resize all fire this too).
      if (performance.now() - lastInputAt.current > INPUT_GRACE_MS) return
      const awayFromEdge = range.to < pointCount.current - 3
      atLiveEdge.current = !awayFromEdge
      setShowBackToLive(awayFromEdge)
    }
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRange)

    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width
      if (w && chartRef.current) chartRef.current.applyOptions({ width: Math.floor(w) })
    })
    ro.observe(el)

    return () => {
      ro.disconnect()
      el.removeEventListener('wheel', markInput)
      el.removeEventListener('pointerdown', markInput)
      el.removeEventListener('touchstart', markInput)
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange)
      chart.remove()
      chartRef.current = null
      lineRef.current = null
      oorRef.current = null
      pointCount.current = 0
      didInitialFit.current = false
      atLiveEdge.current = true
      setShowBackToLive(false)
    }
  }, [expanded])

  // a series / range switch (or a fresh expand) re-fits the view next data pass
  useEffect(() => {
    didInitialFit.current = false
    atLiveEdge.current = true
    setShowBackToLive(false)
  }, [series, rangeMs, expanded])

  // feed the selected series on every websocket batch / mode / range change
  useEffect(() => {
    if (!expanded || !chartRef.current) return

    const nowT = frames.length ? (frames[frames.length - 1].time as number) : 0
    const cutoff = rangeMs === Infinity ? -Infinity : nowT - rangeMs / 1000
    const win = frames.filter((f) => (f.time as number) >= cutoff)

    const pts = samplesFor(win, series)
    let lineData: (LineData | WhitespaceData)[] = pts
    let oorData: LineData[] = []

    if (mode === 'smoothed' && pts.length > 0) {
      const s = smooth(pts.map((p) => p.value))
      lineData = pts.map((p, i) => ({ time: p.time, value: s[i] }))
    }

    if (series === 'tof') {
      const oorTimes = win.filter((f) => f.tof != null && f.tofOor).map((f) => f.time)
      // dots ride the current in-range ceiling (autoscaleInfoProvider keeps them
      // out of the scale maths); the line gets a whitespace gap at each OOR time
      // so it breaks instead of plunging to a fake ~8190 mm.
      const ceiling = pts.length ? Math.max(...pts.map((p) => p.value)) : 0
      oorData = oorTimes.map((time) => ({ time, value: ceiling }))
      lineData = [...lineData, ...oorTimes.map((time) => ({ time }) as WhitespaceData)].sort(
        (x, y) => (x.time as number) - (y.time as number),
      )
    }

    minSpanRef.current = SERIES_META[series].minSpan
    lineRef.current?.applyOptions({ color: SERIES_META[series].color })
    lineRef.current?.setData(lineData)
    oorRef.current?.setData(oorData)
    pointCount.current = lineData.length

    if (!didInitialFit.current && lineData.length > 0) {
      chartRef.current.timeScale().fitContent()
      didInitialFit.current = true
    } else if (atLiveEdge.current) {
      chartRef.current.timeScale().scrollToRealTime()
    }
  }, [frames, mode, expanded, series, rangeMs])

  const backToLive = () => {
    chartRef.current?.timeScale().fitContent()
    atLiveEdge.current = true
    setShowBackToLive(false)
  }

  const cards = SERIES_KEYS.filter((k) => available[k])
  const meta = SERIES_META[series]

  return (
    <section className="card live-chart">
      <header className="card-head">
        <h2>Live · node {nodeId}</h2>
        <span className={`ws-badge ${status}`}>{status}</span>
      </header>

      {last ? (
        <p className="muted small">
          latest: seq {last.seq_num} · {new Date(last.ts).toLocaleTimeString()}
        </p>
      ) : (
        <p className="muted small">waiting for frames…</p>
      )}

      <div className="lc-toolbar">
        <div className="lc-toggle" role="group" aria-label="value display mode">
          <button
            className={mode === 'raw' ? 'chip active' : 'chip'}
            onClick={() => setMode('raw')}
          >
            Raw
          </button>
          <button
            className={mode === 'smoothed' ? 'chip active' : 'chip'}
            onClick={() => setMode('smoothed')}
          >
            Smoothed
          </button>
        </div>
        <button
          className={expanded ? 'chip active' : 'chip'}
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? '▴ Hide chart' : '▾ View chart'}
        </button>
      </div>

      {/* compact readout -- the default Live view (issue #2) */}
      <div className="lc-stats">
        {cards.length === 0 && <p className="muted small">no sensor values yet…</p>}
        {cards.map((k) => {
          const oor = k === 'tof' && current.tofOor
          return (
            <div className="lc-stat" key={k}>
              <span className="lc-stat-val">
                {oor ? 'out of range' : fmtValue(k, current[k] as number)}
                {!oor && <span className="lc-stat-unit"> {SERIES_META[k].unit}</span>}
              </span>
              <span className="lc-stat-label">
                {SERIES_META[k].label}
                {mode === 'smoothed' ? ' · smoothed' : ''}
              </span>
            </div>
          )
        })}
      </div>

      {expanded && (
        <>
          <div className="lc-toolbar">
            <div className="lc-toggle" role="group" aria-label="charted series">
              {SERIES_KEYS.map((k) => (
                <button
                  key={k}
                  className={series === k ? 'chip active' : 'chip'}
                  disabled={!available[k]}
                  onClick={() => setSeries(k)}
                >
                  {SERIES_META[k].label}
                </button>
              ))}
            </div>
            <div className="lc-toggle" role="group" aria-label="time range">
              {RANGES.map((r) => (
                <button
                  key={r.label}
                  className={rangeMs === r.ms ? 'chip active' : 'chip'}
                  onClick={() => setRangeMs(r.ms)}
                >
                  {r.label}
                </button>
              ))}
            </div>
            {showBackToLive && (
              <button className="chip lc-live-btn" onClick={backToLive}>
                ↧ back to live
              </button>
            )}
          </div>

          <div ref={containerRef} className="lc-canvas" />

          <div className="lc-legend muted small">
            <span>
              <i style={{ background: meta.color }} /> {meta.label} ({meta.unit})
              {mode === 'smoothed' ? ` · median-${MEDIAN_WINDOW} + MA-${MA_WINDOW}` : ''}
            </span>
            {series === 'tof' && (
              <span>
                <i style={{ background: '#dc2626' }} /> out of range
              </span>
            )}
          </div>
        </>
      )}
    </section>
  )
}
