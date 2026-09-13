import { BrowserWindow, nativeTheme, shell } from 'electron'
import { is } from '@electron-toolkit/utils'
import path from 'node:path'

/** Matches --background in renderer/src/theme/palettes.ts so the first paint
 *  before React mounts is not a white flash in dark mode. */
const BACKGROUND = { dark: '#060608', light: '#f4f5ee' }
const TRANSPARENT = '#00000000'

/**
 * What the page paints over. With vibrancy on, the page must be transparent
 * or the NSVisualEffectView never shows: Electron only defaults to a clear
 * page for a vibrant window when no backgroundColor is given, and an opaque
 * colour chosen at creation (dark, say) then sits under the sidebar's 62%
 * tint forever, which in light mode reads as a muddy grey. With vibrancy
 * off, the opaque colour follows the OS appearance so a theme switch never
 * leaves the wrong ground behind the renderer.
 */
function pageBackground(reduceTransparency: boolean): string {
  if (!reduceTransparency) return TRANSPARENT
  return nativeTheme.shouldUseDarkColors ? BACKGROUND.dark : BACKGROUND.light
}

export function createMainWindow(
  opts: { reduceTransparency?: boolean; uiScale?: number } = {},
): BrowserWindow {
  const reduceTransparency = opts.reduceTransparency ?? false
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    show: false,
    backgroundColor: pageBackground(reduceTransparency),
    // Full-height translucent sidebar like Finder/Notes: the window blurs the
    // desktop behind it and the renderer keeps the sidebar column
    // semi-transparent (see tokens.css .mac-sidebar) while content stays opaque.
    // "Reduce transparency" in Settings turns the effect off (applyVibrancy).
    vibrancy: reduceTransparency ? undefined : 'sidebar',
    visualEffectState: 'active',
    // Native traffic lights sit inside the sidebar's top padding (Sidebar.tsx).
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 18, y: 18 },
    webPreferences: {
      // .cjs on purpose: see the preload section of electron.vite.config.ts.
      preload: path.join(__dirname, '../preload/index.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })

  win.once('ready-to-show', () => win.show())
  // Zoom is per-load state in Chromium: reapply after every navigation so a
  // dev reload does not snap back to 100%.
  win.webContents.on('did-finish-load', () => {
    win.webContents.setZoomFactor((opts.uiScale ?? 100) / 100)
  })

  // External links open in the default browser, never inside the shell.
  win.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url)
    return { action: 'deny' }
  })

  if (is.dev && process.env.ELECTRON_RENDERER_URL) {
    void win.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    void win.loadFile(path.join(__dirname, '../renderer/index.html'))
  }
  return win
}

/** Settings > Appearance > UI scale, as Chromium's zoom factor on every window. */
export function applyUiScale(percent: number): void {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) win.webContents.setZoomFactor(percent / 100)
  }
}

/** Mirror the "Reduce transparency" setting onto every open window. */
export function applyVibrancy(reduceTransparency: boolean): void {
  for (const win of BrowserWindow.getAllWindows()) {
    if (win.isDestroyed()) continue
    win.setVibrancy(reduceTransparency ? null : 'sidebar')
  }
  applyPageBackground(reduceTransparency)
}

/** Re-ground every window after a vibrancy or OS appearance change. */
export function applyPageBackground(reduceTransparency: boolean): void {
  const color = pageBackground(reduceTransparency)
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) win.setBackgroundColor(color)
  }
}
