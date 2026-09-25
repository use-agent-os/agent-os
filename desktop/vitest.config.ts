/// <reference types="vitest/config" />
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// Same alias map as electron.vite.config.ts (renderer target).
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '~': path.resolve(__dirname, 'src/renderer/src'),
      '@': path.resolve(__dirname, '../frontend/src'),
      '@shared': path.resolve(__dirname, 'src/shared'),
    },
    dedupe: [
      'react',
      'react-dom',
      'zustand',
      'sonner',
      '@tanstack/react-query',
      'motion',
      'lucide-react',
    ],
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['src/renderer/src/test/setup.ts'],
    globals: false,
    passWithNoTests: true,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
