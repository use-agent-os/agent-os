/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// Dev gateway target: override with AGENTOS_GATEWAY (e.g. http://127.0.0.1:18999)
// when the default gateway port is taken by another instance.
const gateway = process.env.AGENTOS_GATEWAY ?? 'http://127.0.0.1:18791'
const gatewayWs = gateway.replace(/^http/, 'ws')

// Prod build: assets are served by the gateway's existing static mount, so the
// built index.html must reference them under {base_path}/static/dist/.
// Dev server: serve under /control/ so the router basename (/control) and the
// bootstrap URL (/control/api/bootstrap) line up exactly like production.
// Custom base_path support is a cutover-plan item (see parity matrix).
export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss()],
  base: command === 'serve' ? '/control/' : '/control/static/dist/',
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  build: {
    outDir: path.resolve(__dirname, '../src/agentos/gateway/static/dist'),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/ws': { target: gatewayWs, ws: true },
      '/control/api': { target: gateway, changeOrigin: true },
      // Approval REST routes are registered at the gateway ROOT (app.py:535-537),
      // NOT under /control — the approval-monitor service polls GET /api/approvals
      // and POSTs /api/approvals/resolve. Without this the dev server 404s them
      // and the approvals view / global prompt appear broken.
      '/api': { target: gateway, changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['src/test/setup.ts'],
    globals: false,
    passWithNoTests: true,
  },
}))
