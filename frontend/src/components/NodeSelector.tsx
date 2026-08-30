/**
 * NodeSelector -- choose which node the dashboard is focused on.
 *
 * Props:
 *   value: number | null            currently selected node_id (null = all)
 *   onChange: (id: number | null) => void
 *
 * Data: GET /nodes  ->  [{ node_id, last_seen, status }]
 * Renders each node with an online/offline dot; an "All nodes" option maps to null.
 *
 * TODO:
 *  - [ ] fetch /nodes on mount + poll (e.g. every 10s) for status changes
 *  - [ ] loading / error states
 *  - [ ] highlight offline nodes
 */

type Props = {
  value: number | null
  onChange: (id: number | null) => void
}

export function NodeSelector(_props: Props) {
  // TODO: implement -- see docstring.
  return null
}
