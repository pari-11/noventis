# Noventis — design-sync notes

## Shape / scope
- This repo's `frontend/` is an **application**, not a component library. The sync is
  deliberately **tokens-only**: `styles.css` + `_ds_bundle.css` (a copy of
  `frontend/src/index.css`) + an empty-bodied `_ds_bundle.js`. Zero components.
- `cfg.componentSrcMap` excludes all 5 app components (`App`, `NodeSelector`,
  `LiveChart`, `HistoryPanel`, `NodeSummaryCard`) so the package adapter takes its
  `[ZERO_MATCH] -> tokens-only` path.

## Build
- Run from **repo root**. `--node-modules ./frontend/node_modules`.
- `cfg.entry` points at `frontend/src/_ds-bundle-entry.ts` — a committed 1-line
  `export {}` stub that exists ONLY so the converter can resolve `PKG_DIR` (walks up
  to `frontend/package.json`) and emit an empty IIFE. **Do not** point the entry at
  `src/main.tsx` — that bundles the whole app (281 KB) and runs `createRoot().render()`
  on load.
- No Storybook. `shape: "package"` pinned in config.
- Render check: skipped with `--no-render-check` (0 previews — nothing to render).
  Recorded known-clean, not a new warn.

## Re-sync risks
- If `frontend/src/index.css` gains component-specific classes, `.design-sync/conventions.md`
  can drift — the re-sync validates every cited name against the fresh `_ds_bundle.css`
  and reports misses. `#f0f1f4` was cut once (it lives in LiveChart JS, not the CSS).
- If someone deletes `frontend/src/_ds-bundle-entry.ts`, every build fails at PKG_DIR
  resolution. It's committed for this reason.
- `pkgJson.version` is `0.0.0` (private app) → README shows `@0.0.0`. Expected.
