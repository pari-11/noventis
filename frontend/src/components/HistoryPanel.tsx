/**
 * HistoryPanel -- historical readings for the selected node.
 *
 * Props:
 *   nodeId: number | null
 *
 * Data: GET /readings?node_id=<nodeId>&since=...&limit=...
 * Renders a paginated table / mini-chart of past readings. Disabled (or shows a
 * hint) when nodeId is null, since /readings requires a node_id.
 *
 * TODO:
 *  - [ ] fetch on nodeId change; time-range picker (last 1h / 24h / 7d)
 *  - [ ] pagination (keyset: pass ?before=<timestamp>)
 *  - [ ] column set mirrors docs/protocol-spec.md section 3 decoded keys
 *  - [ ] CSV export of the current view
 *  - [ ] link out to /raw-frames for the same node (forensic view)
 */

type Props = {
  nodeId: number | null
}

export function HistoryPanel(_props: Props) {
  // TODO: implement -- see docstring.
  return null
}
