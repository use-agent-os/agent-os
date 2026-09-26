import { BrowserWindow, nativeTheme, shell } from 'electron'
import { is } from '@electron-toolkit/utils'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

/** The only schemes a link in the renderer may hand to the OS. */
const EXTERNAL_SCHEMES = new Set(['http:', 'https:', 'mailto:'])

/**
 * May this URL leave the app for the default browser or mail client? Only
 * web and mail links; `file:`, `javascript:`, `data:` and custom schemes are
 * refused outright, since a rendered link is content the agent (or a token
 * name it read from the chain) may have written.
 */
export function isAllowedExternalUrl(url: string): boolean {
  try {
    return EXTERNAL_SCHEMES.has(new URL(url).protocol)
  } catch {
    return false
  }
}

/**
 * The renderer's own place: the dev server's origin, or the `file:` URL of
 * the built renderer directory. `isAppNavigation` measures against it.
 */
export function appOriginFor(rendererUrl: string | undefined, rendererDir: string): string {
  if (rendererUrl) {
    try {
      return new URL(rendererUrl).origin
    } catch {
      // Fall through to the packaged renderer.
    }
  }
  return pathToFileURL(path.join(rendererDir, '/')).href
}

/**
 * Is this navigation still inside the app? For a dev-server origin the
 * origin must match exactly; for a packaged renderer the URL must be a
 * `file:` URL under the renderer directory. Anything else — another site,
 * another local file, `javascript:`/`data:` — is not the app.
 */
export function isAppNavigation(url: string, appOrigin: string): boolean {
  let target: URL
  try {
    target = new URL(url)
  } catch {
    return false
  }
  if (appOrigin.startsWith('file:')) {
    return target.protocol === 'file:' && target.pathname.startsWith(new URL(appOrigin).pathname)
  }
  return target.origin === appOrigin
}

/** Where this process's renderer lives: the dev server in dev, disk when packaged. */
function rendererLocation(): { rendererUrl: string | undefined; rendererDir: string } {
  return {
    rendererUrl: is.dev ? process.env.ELECTRON_RENDERER_URL : undefined,
    rendererDir: path.join(__dirname, '../renderer'),
  }
}

/**
 * The renderer's own origin (see `appOriginFor`). The navigation guard
 * measures against it, and so does the loopback CORS layer (loopback-cors.ts),
 * which has to answer for the origin Chromium will compare.
 */
export function rendererAppOrigin(): string {
  const { rendererUrl, rendererDir } = rendererLocation()
  return appOriginFor(rendererUrl, rendererDir)
}

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

  // External links open in the default browser, never inside the shell —
  // and only web and mail links go out at all; anything else is dropped.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isAllowedExternalUrl(url)) void shell.openExternal(url)
    return { action: 'deny' }
  })

  // The renderer never leaves its own origin. A link, a form or a script
  // that navigates the top frame anywhere else is stopped here; the page
  // stays where it was. (`loadURL`/`loadFile` from this process do not
  // raise will-navigate, so the app's own loads are unaffected.)
  const { rendererUrl, rendererDir } = rendererLocation()
  const appOrigin = appOriginFor(rendererUrl, rendererDir)
  win.webContents.on('will-navigate', (event, url) => {
    if (!isAppNavigation(url, appOrigin)) event.preventDefault()
  })

  if (rendererUrl) {
    void win.loadURL(rendererUrl)
  } else {
    void win.loadFile(path.join(rendererDir, 'index.html'))
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
