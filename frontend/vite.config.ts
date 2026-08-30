import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The FastAPI backend (uvicorn) listens on IPv4 127.0.0.1:8000 by default.
// Use the numeric address, not "localhost": on modern Node/Windows "localhost"
// resolves to IPv6 ::1 first, and uvicorn is not listening there -> the proxy
// fails with `connect ECONNREFUSED ::1:8000`.
// Override with:  VITE_BACKEND=http://<host>:<port> npm run dev
const BACKEND = process.env.VITE_BACKEND ?? 'http://127.0.0.1:8000'
const WS_BACKEND = BACKEND.replace(/^http/, 'ws')

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/health': BACKEND,
      '/nodes': BACKEND,
      '/readings': BACKEND,
      '/raw-frames': BACKEND,
      '/live': { target: WS_BACKEND, ws: true },
    },
  },
})
