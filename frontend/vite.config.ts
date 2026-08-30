import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies the REST + WebSocket API to the FastAPI backend on :8000,
// so the frontend can use same-origin relative paths ("/nodes", "/live", ...).
//
// TODO:
//  - [ ] Confirm backend port / host; make it configurable via env if needed.
//  - [ ] Add a build `base` if the app is served under a sub-path.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/health': 'http://localhost:8000',
      '/nodes': 'http://localhost:8000',
      '/readings': 'http://localhost:8000',
      '/raw-frames': 'http://localhost:8000',
      '/live': { target: 'ws://localhost:8000', ws: true },
    },
  },
})
