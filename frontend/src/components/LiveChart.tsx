/**
 * LiveChart -- real-time plot of incoming telemetry for the selected node.
 *
 * Props:
 *   nodeId: number | null           null = all nodes (series keyed by node_id)
 *
 * Consumes useWebSocket(nodeId); appends each LiveMessage to a bounded, rolling
 * window (e.g. last 5 min or last N points) and renders one line per metric
 * (tof_dist_mm, |accel|, etc.).
 *
 * TODO:
 *  - [ ] pick a charting lib (see frontend/package.json) and add it
 *  - [ ] rolling buffer with a max length; drop oldest
 *  - [ ] metric selector (which decoded keys to plot)
 *  - [ ] derive scalar magnitudes from accel_mg / gyro_cdps vectors
 *  - [ ] pause-on-hover / "live" toggle
 */

type Props = {
  nodeId: number | null
}

export function LiveChart(_props: Props) {
  // TODO: implement -- see docstring.
  return null
}
