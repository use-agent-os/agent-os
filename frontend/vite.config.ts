/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// Assets are served by the gateway's existing static mount, so the built
// index.html must reference them under {base_path}/static/dist/.
// Custom base_path support is a cutover-plan item (see parity matrix).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/control/static/dist/',
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  build: {
    outDir: path.resolve(__dirname, '../src/agentos/gateway/static/dist'),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/ws': { target: 'ws://127.0.0.1:18791', ws: true },
      '/control/api': { target: 'http://127.0.0.1:18791', changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['src/test/setup.ts'],
    globals: false,
    passWithNoTests: true,
  },
})
