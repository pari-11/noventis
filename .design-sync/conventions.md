## Noventis dashboard — styling conventions

This "design system" ships **no components and no JS runtime** — `window.NoventisFrontend`
is empty. It is purely the Noventis telemetry dashboard's **visual language**: a set of
CSS custom properties plus a small hand-rolled class vocabulary. Build with plain
React/JSX elements and these classes; there is nothing to import from the bundle.

### Setup

Link `styles.css` once. No provider, no wrapper, no theme context. The `@import`
closure defines everything:

- `:root` declares the 8 color tokens and the base `font-family`.
- `body` is painted `background: var(--bg); color: var(--text)`; `* { box-sizing: border-box }`.

Put app content inside `<div class="app">` (max-width 1100px, centered, 20px padding).

### Tokens — the only palette (`var(--*)`)

| Token | Value | Role |
|---|---|---|
| `--bg` | `#f6f7f9` | page background |
| `--card` | `#ffffff` | raised surface |
| `--border` | `#e2e5ea` | hairline borders |
| `--text` | `#1c2024` | primary text |
| `--muted` | `#6b7280` | secondary text |
| `--accent` | `#2563eb` | interactive / selected (blue) |
| `--up` | `#16a34a` | good / online / live (green) |
| `--down` | `#dc2626` | bad / offline / error (red) |

There is **no spacing, radius, or typography scale** — those are literal `px` in the
classes below. If you need a new semantic color, add a `--*` token; do not introduce
more bare hex.

### Class vocabulary (all defined in `_ds_bundle.css`)

**Surfaces & layout**
- `.card` — white, `1px solid var(--border)`, `border-radius: 12px`, `padding: 16px`. The base container.
- `.card-head` — flex row, `space-between`, `gap: 8px`; put an `<h2>` (15px) and an action on it.
- `.grid` — 1 column, `gap: 16px`; becomes 2 equal columns at `min-width: 900px`.
- `.summary-grid` — `repeat(auto-fill, minmax(220px, 1fr))`, `gap: 12px`.

**Pills & badges** (all `border-radius: 999px`)
- `.pill` — neutral status pill; modifiers `.pill.up` (green on `#e7f6ec`), `.pill.down` (red on `#fdeaea`).
- `.ws-badge` — uppercase connection badge; modifiers `.open` / `.connecting` / `.closed`.
- `.chip` — pill-shaped `<button>` (`1px solid var(--border)`, `background: var(--card)`); `.chip.active` = accent border + `#eef3ff` bg + accent text; `.chip:disabled` = 50% opacity.

**Indicators**
- `.dot` — 8px circle; `.dot.live` (green), `.dot.stale` (grey `#cbd0d8`).
- `.auto-tag` — tiny inline "auto" label with a `.dot`; add `.pulsing` to flash the dot green.

**Text utilities**
- `.muted` (→ `var(--muted)`), `.small` (`font-size: 12px`), `.error` (→ `var(--down)`).

**Tables** — style bare `table` / `th` / `td` (12px, `tabular-nums`, bottom-border rows). Wrap in `.table-wrap` for horizontal scroll.

Component-specific families also present (reuse only if building the same widgets):
`.lc-toolbar` / `.lc-toggle` / `.lc-canvas` (320px) / `.lc-legend` / `.lc-live-btn` and the
compact readout tiles `.lc-stats` / `.lc-stat` / `.lc-stat-val` / `.lc-stat-unit` /
`.lc-stat-label` for the live chart panel; `.summary-card` / `.sc-top` / `.sc-id` /
`.sc-badge` / `.sc-tof` / `.sc-tof-val` / `.sc-tof-oor` / `.sc-unit` / `.sc-meta` for a node
summary card; `.history-scroll` (capped-height scroll body with sticky header) for tables.

Non-token literals you'll see in the source (do **not** copy as new "colors"):
`#eef0f3` neutral bg, `#eef3ff` active-chip bg, `#e7f6ec` / `#fdeaea` success/error bg,
`#fef6e7` + `#b45309` warning, `#c7ccd4` chip hover border, `#fbfbfc` inset-tile tint.

### Where the truth lives

`_ds_bundle.css` (reached from `styles.css` via `@import`) is the **entire** stylesheet —
read it before styling. There are no per-component docs.

### Idiomatic snippet

```jsx
<section className="card">
  <header className="card-head">
    <h2>Node 1</h2>
    <span className="pill up">online</span>
  </header>
  <p className="muted small">last seen 2s ago</p>
  <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
    <button className="chip active">Raw</button>
    <button className="chip">Smoothed</button>
  </div>
</section>
```

Library control = a real element with the DS class; layout glue (the `flex`/`gap` above)
is your own inline style or a `.grid` / `.summary-grid` wrapper.
