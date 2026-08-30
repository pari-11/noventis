/**
 * App.tsx -- Noventis dashboard shell.
 *
 * Layout:
 *   <NodeSelector>   pick a node, or "all"
 *   <LiveChart>      streaming values for the selected node (useWebSocket -> /live)
 *   <HistoryPanel>   paginated history for the selected node (REST /readings)
 *
 * The selected node_id is owned here and passed to all three children.
 * `null` node_id means "all nodes".
 *
 * TODO:
 *  - [ ] useState<number | null>(null) for selectedNodeId, lift to children
 *  - [ ] fetch GET /nodes for the selector options
 *  - [ ] basic error / loading / empty states
 *  - [ ] layout + styling (grid: selector on top, chart + history below)
 */

export default function App() {
  // TODO: scaffold the real layout.
  return null
}
