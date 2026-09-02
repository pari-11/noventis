/**
 * LiveChart -- one node's live telemetry.
 *
 * DEFAULT VIEW is a compact readout: the current value (raw or smoothed, per the
 * toggle) of every active sensor -- ToF (mm), |acceleration| (m/s^2),
 * |angular rate| (rad/s) -- as plain stat tiles, no chart rendered.
 *
 * "Show chart" expands a zoomable lightweight-charts (v4) plot of ONE series at a
 * time (ToF / |accel| / |gyro|, chosen with the in-chart selector). The three
 * quantities live on wildly different scales (ToF in hundreds of mm, accel in
 * single-digit m/s^2, gyro in fractional rad/s) and do not share a y-axis
 * meaningfully, so only the selected series is drawn. A time-range selector
 * (1 min / 5 min / All) trims what is charted. Collapsing back to the compact
 * view keeps the raw/smoothed, series and time-range selections for next expand
 * (they are React state on this always-mounted component); only the chart's
 * pan/zoom is rebuilt.
 *
 * Chart colours are read from the CSS design tokens (`--card`, `--muted`,
 * `--grid`, `--accent`, ...) at creation time; a MutationObserver on
 * <html data-theme> rebuilds the chart when the light/dark theme changes so it
 * never renders light-on-dark.
 *
 * Time axis: lightweight-charts renders numeric times in UTC by default, which
 * disagreed visibly with the "latest:" text (browser local time). Both the axis
 * tick labels (timeScale.tickMarkFormatter) and the crosshair tooltip
 * (localization.timeFormatter) now format via Intl.DateTimeFormat with no
 * explicit timeZone -> the browser's local zone, matching "latest:".
 *
 * ToF handling (see backend TOF_MAX_VALID_MM): the VL53L0X "no target" sentinel
 * (~8190 mm) arrives flagged `tof_out_of_range`. Those points are NOT fed into
 * the ToF line as a distance -- in BOTH raw and smoothed views the line breaks
 * (whitespace gap) and a separate out-of-range overlay series (its own `--down`
 * colour, never the ToF / smoothed line colour) is drawn instead: dots for lone
 * sentinels, a joined segment for consecutive ones. One bogus reading can't
 * blow out the y-axis.
 *
 * Distance threshold alert: a collapsible min/max mm control, toggled from a
 * button inline with the Raw/Smoothed toggle. While open, if the live ToF value
 * falls outside [min, max] the ToF stat tile's value is drawn in the secondary
 * accent colour. Pure client-side comparison against the already-streaming
 * value -- no backend involvement.
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
// the node is "Online" only while frames are still arriving; once this long
// passes with no new frame it flips to "Offline" (mirrors the backend's
// per-node stale window)
const NODE_ONLINE_AFTER_MS = 15_000
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
const SERIES_META: Record<SeriesKey, { label: string; unit: string; minSpan: number }> = {
  tof: { label: 'ToF', unit: 'mm', minSpan: 20 },
  accel: { label: '|accel|', unit: 'm/s²', minSpan: 2 },
  gyro: { label: '|gyro|', unit: 'rad/s', minSpan: 1 },
}
const SERIES_KEYS = Object.keys(SERIES_META) as SeriesKey[]

const RANGES: { label: string; ms: number }[] = [
  { label: '1 min', ms: 60_000 },
  { label: '5 min', ms: 300_000 },
  { label: 'All', ms: Infinity },
]

// Chart palette pulled from the design tokens so the plot tracks the light/dark
// theme. Values are read from :root at call time; the fallbacks are the former
// hard-coded light-theme colours.
type ChartColors = {
  bg: string
  axis: string
  grid: string
  border: string
  accent2: string
  oor: string
  tof: string
  accel: string
  gyro: string
}
function readChartColors(): ChartColors {
  const fb: ChartColors = {
    bg: '#ffffff',
    axis: '#6b7280',
    grid: '#f0f1f4',
    border: '#e2e5ea',
    accent2: '#8b5cf6',
    oor: '#dc2626',
    tof: '#2563eb',
    accel: '#16a34a',
    gyro: '#d97706',
  }
  if (typeof window === 'undefined') return fb
  const cs = getComputedStyle(document.documentElement)
  const v = (name: string, f: string) => cs.getPropertyValue(name).trim() || f
  return {
    bg: v('--card', fb.bg),
    axis: v('--muted', fb.axis),
    grid: v('--grid', fb.grid),
    border: v('--border', fb.border),
    accent2: v('--accent-2', fb.accent2),
    oor: v('--down', fb.oor),
    tof: v('--accent', fb.tof),
    accel: v('--up', fb.accel),
    gyro: v('--accent-2', fb.gyro),
  }
}
const lineColorFor = (c: ChartColors, key: SeriesKey, mode: Mode) =>
  mode === 'smoothed' ? c.accent2 : c[key]

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

const Icon = ({ d, size = 14 }: { d: string; size?: number }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d={d} />
  </svg>
)
const I = {
  chevronDown: 'M6 9l6 6 6-6',
  chevronUp: 'M18 15l-6-6-6 6',
  sliders: 'M4 7h10M18 7h2M4 17h4M12 17h8M16 4v6M8 14v6',
}

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

type Props = { nodeId: number; nodeName?: string }

export function LiveChart({ nodeId, nodeName }: Props) {
  const { messages, last } = useWebSocket(nodeId, { bufferSize: WS_BUFFER })

  // all selections are plain state on this always-mounted component, so
  // collapsing the chart never loses them (issue #2).
  const [mode, setMode] = useState<Mode>('raw')
  const [expanded, setExpanded] = useState(false)
  const [series, setSeries] = useState<SeriesKey>('tof')
  const [rangeMs, setRangeMs] = useState<number>(RANGES[1].ms)

  // 1 Hz clock so the Online/Offline badge can flip on its own once frames stop
  // (state only changes on a new frame otherwise)
  const [nowTs, setNowTs] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNowTs(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])
  const online = last != null && nowTs - Date.parse(last.ts) < NODE_ONLINE_AFTER_MS

  // distance threshold alert (client-side only)
  const [thrOpen, setThrOpen] = useState(false)
  const [thr, setThr] = useState<{ min: number; max: number }>({ min: 20, max: 35 })

  // bump on a light/dark theme flip so the chart-creation effect re-runs
  const [themeTick, setThemeTick] = useState(0)
  useEffect(() => {
    const obs = new MutationObserver(() => setThemeTick((n) => n + 1))
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => obs.disconnect()
  }, [])

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

  // ToF outside the alert band (only meaningful while the panel is open and we
  // have a real in-range value)
  const tofAlert =
    thrOpen && !current.tofOor && current.tof != null && (current.tof < thr.min || current.tof > thr.max)

  // While collapsed, keep the readout pointed at a series that actually has
  // data (e.g. only ToF is arriving). Once the chart is expanded we respect an
  // explicit tile pick even if that series has no data yet -- the chart then
  // shows a "waiting for data" state instead of yanking the selection away.
  useEffect(() => {
    if (expanded) return
    if (!available[series]) {
      const first = SERIES_KEYS.find((k) => available[k])
      if (first) setSeries(first)
    }
  }, [available, series, expanded])

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

  // create / tear down the chart with the expanded view (or a theme flip)
  useEffect(() => {
    if (!expanded) return
    const el = containerRef.current
    if (!el) return

    const c = readChartColors()

    const chart = createChart(el, {
      width: el.clientWidth,
      height: CHART_HEIGHT,
      layout: {
        background: { type: ColorType.Solid, color: c.bg },
        textColor: c.axis,
        fontSize: 11,
        attributionLogo: false, // drop the TradingView watermark (v4.2+)
      },
      grid: {
        vertLines: { color: c.grid },
        horzLines: { color: c.grid },
      },
      rightPriceScale: { borderColor: c.border, scaleMargins: { top: 0.15, bottom: 0.15 } },
      timeScale: {
        borderColor: c.border,
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
      color: lineColorFor(c, series, mode),
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
    // ToF out-of-range overlay: always drawn in the dedicated `--down` colour
    // (never the ToF / smoothed line colour), in BOTH raw and smoothed views.
    // Lone sentinels show as dots; consecutive ones join into a short segment.
    // Pinned to the price scale but excluded from its autoscale so they never
    // widen the axis. Empty for accel/gyro.
    oorRef.current = chart.addLineSeries({
      color: c.oor,
      lineWidth: 2,
      lineVisible: true,
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
  }, [expanded, themeTick])

  // a series / range switch (or a fresh expand) re-fits the view next data pass
  useEffect(() => {
    didInitialFit.current = false
    atLiveEdge.current = true
    setShowBackToLive(false)
  }, [series, rangeMs, expanded])

  // feed the selected series on every websocket batch / mode / range change
  useEffect(() => {
    if (!expanded || !chartRef.current) return

    const c = readChartColors()
    const nowT = frames.length ? (frames[frames.length - 1].time as number) : 0
    const cutoff = rangeMs === Infinity ? -Infinity : nowT - rangeMs / 1000
    const win = frames.filter((f) => (f.time as number) >= cutoff)

    const pts = samplesFor(win, series)
    let lineData: (LineData | WhitespaceData)[] = pts
    let oorData: (LineData | WhitespaceData)[] = []

    if (mode === 'smoothed' && pts.length > 0) {
      const s = smooth(pts.map((p) => p.value))
      lineData = pts.map((p, i) => ({ time: p.time, value: s[i] }))
    }

    if (series === 'tof') {
      // Build the out-of-range overlay from the raw frames, independent of the
      // raw/smoothed mode: consecutive sentinels join into a `--down` segment,
      // isolated ones stay dots (a whitespace point breaks the overlay between
      // separate runs). Dots/segments ride the current in-range ceiling
      // (autoscaleInfoProvider keeps them out of the scale maths). The main line
      // (raw OR smoothed) gets a whitespace gap at every OOR time so it breaks
      // instead of plunging to a fake ~8190 mm.
      const ceiling = pts.length ? Math.max(...pts.map((p) => p.value)) : 0
      const gapTimes: UTCTimestamp[] = []
      let prevOor = false
      for (const f of win) {
        if (f.tof == null) continue
        if (f.tofOor) {
          if (!prevOor && oorData.length) oorData.push({ time: f.time } as WhitespaceData)
          oorData.push({ time: f.time, value: ceiling })
          gapTimes.push(f.time)
          prevOor = true
        } else {
          prevOor = false
        }
      }
      lineData = [...lineData, ...gapTimes.map((time) => ({ time }) as WhitespaceData)].sort(
        (x, y) => (x.time as number) - (y.time as number),
      )
    }

    minSpanRef.current = SERIES_META[series].minSpan
    lineRef.current?.applyOptions({ color: lineColorFor(c, series, mode) })
    oorRef.current?.applyOptions({ color: c.oor })
    lineRef.current?.setData(lineData)
    oorRef.current?.setData(oorData)
    pointCount.current = lineData.length

    if (!didInitialFit.current && lineData.length > 0) {
      chartRef.current.timeScale().fitContent()
      didInitialFit.current = true
    } else if (atLiveEdge.current) {
      chartRef.current.timeScale().scrollToRealTime()
    }
  }, [frames, mode, expanded, series, rangeMs, themeTick])

  const backToLive = () => {
    chartRef.current?.timeScale().fitContent()
    atLiveEdge.current = true
    setShowBackToLive(false)
  }

  const meta = SERIES_META[series]
  const legendColor = lineColorFor(readChartColors(), series, mode)
  // chart is open on a series that has no samples yet -> show a waiting state
  const chartWaiting = expanded && !available[series]

  return (
    <section className="card live-chart">
      <header className="card-head">
        <h2>{nodeName ?? `Node ${nodeId}`}</h2>
        <span className={online ? 'ws-badge open' : 'ws-badge'}>{online ? 'Online' : 'Offline'}</span>
      </header>

      {last ? (
        <p className="muted small mono">
          latest: seq {last.seq_num} · {new Date(last.ts).toLocaleTimeString()}
        </p>
      ) : (
        <p className="muted small">waiting for frames…</p>
      )}

      <div className="lc-toolbar">
        <div className="lc-toolbar-left">
          <div className="seg" role="group" aria-label="value display mode">
            <button className={mode === 'raw' ? 'on' : ''} onClick={() => setMode('raw')}>
              Raw
            </button>
            <button
              className={mode === 'smoothed' ? 'on alt' : ''}
              onClick={() => setMode('smoothed')}
            >
              Smoothed
            </button>
          </div>
          {available.tof && (
            <button
              type="button"
              className={thrOpen ? 'btn thr-btn accent' : 'btn thr-btn'}
              aria-expanded={thrOpen}
              onClick={() => setThrOpen((o) => !o)}
              title="ToF distance threshold alert"
            >
              <Icon d={I.sliders} />
              Threshold
            </button>
          )}
        </div>
        <button className="btn" aria-expanded={expanded} onClick={() => setExpanded((v) => !v)}>
          <Icon d={expanded ? I.chevronUp : I.chevronDown} />
          {expanded ? 'Hide chart' : 'Show chart'}
        </button>
      </div>

      {/* compact readout -- also the chart's series selector: clicking a tile
          picks that series and expands the chart (there is no separate series
          toggle). All three tiles always render; one with no data yet shows
          "--", is de-emphasised, and still selects its series when clicked. */}
      <div className="lc-stats">
        {SERIES_KEYS.map((k) => {
          const oor = k === 'tof' && current.tofOor
          const hasData = available[k]
          const sel = expanded && series === k
          const warn = k === 'tof' && tofAlert
          const empty = !hasData && !oor
          const cls =
            'lc-stat lc-stat-btn' +
            (sel ? ' lc-stat-sel' : '') +
            (sel && mode === 'smoothed' ? ' alt' : '') +
            (warn ? ' warn' : '') +
            (empty && !sel ? ' lc-stat-empty' : '')
          return (
            <button
              type="button"
              className={cls}
              key={k}
              aria-pressed={sel}
              title={
                oor
                  ? `${SERIES_META[k].label} — out of range`
                  : hasData
                    ? `Plot ${SERIES_META[k].label}`
                    : `No ${SERIES_META[k].label} data yet — click to watch for it`
              }
              onClick={() => {
                setSeries(k)
                setExpanded(true)
              }}
            >
              <span className="lc-stat-val">
                {/* compact indicator in the value slot so its width never changes */}
                {oor ? 'OOR' : hasData ? fmtValue(k, current[k] as number) : '--'}
                {!oor && <span className="lc-stat-unit"> {SERIES_META[k].unit}</span>}
              </span>
              <span className="lc-stat-label">
                {oor
                  ? 'out of range'
                  : `${SERIES_META[k].label}${hasData && mode === 'smoothed' ? ' · smoothed' : ''}`}
              </span>
            </button>
          )
        })}
      </div>

      {thrOpen && (
        <div className="thr-panel">
          <span className="muted small">ToF alert range</span>
          <label className="thr-field">
            <span className="muted small">min</span>
            <input
              type="number"
              className="num-input"
              value={thr.min}
              onChange={(e) => setThr((t) => ({ ...t, min: Number(e.target.value) }))}
            />
          </label>
          <label className="thr-field">
            <span className="muted small">max</span>
            <input
              type="number"
              className="num-input"
              value={thr.max}
              onChange={(e) => setThr((t) => ({ ...t, max: Number(e.target.value) }))}
            />
          </label>
          <span className="muted small">mm — the ToF value is highlighted while outside this range</span>
        </div>
      )}

      {expanded && (
        <>
          <div className="lc-toolbar">
            <span className="muted small lc-plot-cap" title="click a tile above to change">
              <strong style={{ color: 'var(--text)', fontWeight: 600 }}>{meta.label}</strong>
              {mode === 'smoothed' && <span className="smoothed-note"> · smoothed</span>}
            </span>
            <div className="seg num" role="group" aria-label="time range">
              {RANGES.map((r) => (
                <button
                  key={r.label}
                  className={rangeMs === r.ms ? 'on' : ''}
                  onClick={() => setRangeMs(r.ms)}
                >
                  {r.label}
                </button>
              ))}
            </div>
            {showBackToLive && (
              <button className="btn accent" onClick={backToLive}>
                ↧ back to live
              </button>
            )}
          </div>

          <div className="lc-canvas-wrap">
            <div ref={containerRef} className="lc-canvas" />
            {chartWaiting && (
              <div className="lc-canvas-wait">
                <p className="muted small">waiting for {meta.label} data…</p>
              </div>
            )}
          </div>

          <div className="lc-legend muted small">
            <span>
              <i style={{ background: legendColor }} /> {meta.label} ({meta.unit})
              {mode === 'smoothed' ? ` · median-${MEDIAN_WINDOW} + MA-${MA_WINDOW}` : ''}
            </span>
            {series === 'tof' && (
              <span>
                <i style={{ background: readChartColors().oor }} /> out of range
              </span>
            )}
          </div>
          <p className="muted small">drag to pan · scroll to zoom</p>
        </>
      )}
    </section>
  )
}
