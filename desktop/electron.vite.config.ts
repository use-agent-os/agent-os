import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

const shared = path.resolve(__dirname, 'src/shared')
const rendererSrc = path.resolve(__dirname, 'src/renderer/src')
// The web console's source. The desktop renderer is a second frontend on the
// same gateway: it imports the console's transport, transcript and chat logic
// from here (alias `@/`, the console's own alias) and keeps its UI under `~/`.
const webSrc = path.resolve(__dirname, '../frontend/src')

/**
 * Packages the web modules import that must resolve to ONE copy from this
 * project, never from frontend/node_modules: React (hooks break with two
 * copies), stores, and everything with module-level singletons.
 */
const dedupe = [
  'react',
  'react-dom',
  'react-router',
  'zustand',
  'sonner',
  '@tanstack/react-query',
  'motion',
  'lucide-react',
  'marked',
  'dompurify',
  'highlight.js',
  'lightweight-charts',
  'class-variance-authority',
  '@radix-ui/react-slot',
  'clsx',
  'tailwind-merge',
]

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    resolve: { alias: { '@shared': shared } },
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    resolve: { alias: { '@shared': shared } },
    // The window runs with `sandbox: true`, and sandboxed preload scripts must
    // be CommonJS: Electron will not load an ESM preload into a sandboxed
    // renderer. Keep the package ESM and emit just this bundle as .cjs.
    build: { rollupOptions: { output: { format: 'cjs' } } },
  },
  renderer: {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: { '~': rendererSrc, '@': webSrc, '@shared': shared },
      dedupe,
    },
    // Vite refuses to serve files outside the project root in dev unless the
    // directory is allow-listed.
    server: { fs: { allow: [path.resolve(__dirname, '..')] } },
  },
})
