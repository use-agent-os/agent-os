import { app, BrowserWindow, session } from 'electron'
import { electronApp, optimizer } from '@electron-toolkit/utils'
import { existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import type { DesktopSettings } from '@shared/settings'
import { GatewaySupervisor } from './gateway/supervisor'
import { registerIpc } from './ipc'
import { installAppMenu } from './menu'
import { registerPetScheme, servePets } from './pets/protocol'
import { PetStore } from './pets/store'
import { BootstrapController } from './bootstrap/controller'
import { BootstrapRunner, bundledInstallScript } from './bootstrap/runner'
import { SettingsStore } from './settings/store'
import { AppUpdateController, type UpdaterLike } from './updates/app-updater'
import { defaultMarkerPath, EngineUpdater } from './updates/engine-updater'
import { applyUiScale, applyVibrancy, createMainWindow } from './window'

// Single instance: a second launch focuses the existing window.
if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  const settings = new SettingsStore(path.join(app.getPath('userData'), 'settings.json'))
  const gateway = new GatewaySupervisor(() => settings.get().gateway, {
    logPath: path.join(app.getPath('logs'), 'gateway.log'),
  })
  const pets = new PetStore(path.join(app.getPath('userData'), 'pets'))
  const engineUpdater = new EngineUpdater({
    getSettings: () => settings.get().gateway,
    gateway,
    markerPath: defaultMarkerPath(),
  })
  // Never leave an orphaned gateway behind when the app quits, unless the
  // user asked to keep it running (Settings > General). `stopping` also
  // covers the relaunch an app update performs: the gateway is stopped
  // before quitAndInstall, and the quit hook must not intercept that quit.
  let stopping = false
  const stopGatewayForQuit = async () => {
    stopping = true
    if (gateway.current().pid !== null && settings.get().general.stopGatewayOnQuit) {
      await gateway.stop()
    }
  }
  const appUpdater = new AppUpdateController({
    updater: loadAutoUpdater(),
    version: app.getVersion(),
    beforeInstall: stopGatewayForQuit,
  })
  // First-run engine install: install.sh is bundled next to the asar; in
  // development the repo's copy two levels up is used.
  const agentosHome = path.dirname(defaultMarkerPath()).replace(/\/state\/desktop$/, '')
  const bootstrapRunner = new BootstrapRunner({
    scriptPath:
      bundledInstallScript(process.resourcesPath, path.resolve(__dirname, '../../..')) ??
      path.join(process.resourcesPath, 'install.sh'),
    version: app.getVersion(),
    logDir: path.join(agentosHome, 'logs'),
    cwd: app.getPath('home'),
  })
  const bootstrap = new BootstrapController({
    runner: bootstrapRunner,
    gateway,
    getSettings: () => settings.get().gateway,
    updateSettings: (patch) => settings.update(patch),
    appVersion: app.getVersion(),
  })
  // Custom schemes must be declared before the app is ready.
  registerPetScheme()

  app.on('second-instance', () => {
    const [win] = BrowserWindow.getAllWindows()
    if (win) {
      if (win.isMinimized()) win.restore()
      win.focus()
    }
  })

  app.whenReady().then(() => {
    electronApp.setAppUserModelId('dev.agentos.desktop')
    // A dev run is Electron's own bundle, so its Dock icon is Electron's;
    // the packaged app carries the icon in the bundle.
    if (!app.isPackaged) {
      const icon = path.resolve(__dirname, '../../resources/icon.png')
      if (existsSync(icon)) app.dock?.setIcon(icon)
    }
    // `zoom: true` matters: the default swallows ⌘− and ⌘⇧= in
    // before-input-event, which also silences the View menu's zoom
    // accelerators (Electron drops menu shortcuts for prevented input).
    app.on('browser-window-created', (_, win) =>
      optimizer.watchWindowShortcuts(win, { zoom: true }),
    )

    installLoopbackOriginRewrite()
    servePets(pets)
    registerIpc({
      settings,
      gateway,
      pets,
      engineUpdater,
      appUpdater,
      bootstrapRunner,
      bootstrap,
    })
    installAppMenu(settings)
    createMainWindow(windowOptions(settings.get()))
    mirrorSettingsToOs(settings)
    // The shell is only useful with a gateway behind it. The controller
    // finds the engine and starts the gateway, or offers to install the
    // engine first when this Mac has none (or an older one).
    void bootstrap.launch()
    // A marker from a previous launch means an engine update never finished.
    engineUpdater.recover()

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0)
        createMainWindow(windowOptions(settings.get()))
    })
  })

  // macOS only: closing the last window keeps the app (and any managed
  // gateway) alive in the Dock; Cmd+Q is the way out.
  app.on('window-all-closed', () => {})

  app.on('before-quit', (event) => {
    if (stopping || gateway.current().pid === null) return
    if (!settings.get().general.stopGatewayOnQuit) return
    event.preventDefault()
    void stopGatewayForQuit().finally(() => app.quit())
  })
}

/**
 * electron-updater only has something to update from in a packaged build
 * that electron-builder published (it writes `app-update.yml` next to the
 * asar). Anything else — `electron-vite dev`, a local `package:dir` — is
 * reported as unsupported instead of erroring on every check.
 */
function loadAutoUpdater(): UpdaterLike | null {
  if (!app.isPackaged) return null
  if (!existsSync(path.join(process.resourcesPath, 'app-update.yml'))) return null
  try {
    // Loaded lazily, and through require: the package is CommonJS and reads
    // `app` at import time, so it stays out of the ESM main bundle's imports.
    const { autoUpdater } = createRequire(import.meta.url)('electron-updater') as {
      autoUpdater: UpdaterLike
    }
    return autoUpdater
  } catch {
    return null
  }
}

function windowOptions(s: DesktopSettings): { reduceTransparency: boolean; uiScale: number } {
  return { reduceTransparency: s.appearance.reduceTransparency, uiScale: s.appearance.uiScale }
}

/**
 * Three settings are really window/OS state: the login item, window
 * vibrancy and the zoom factor. Apply them at boot and again on every change
 * so the file and what is on screen agree.
 */
function mirrorSettingsToOs(settings: SettingsStore): void {
  let last: DesktopSettings | null = null
  const apply = (next: DesktopSettings) => {
    if (next.general.openAtLogin !== last?.general.openAtLogin) {
      try {
        if (app.isPackaged || next.general.openAtLogin !== app.getLoginItemSettings().openAtLogin) {
          app.setLoginItemSettings({ openAtLogin: next.general.openAtLogin })
        }
      } catch {
        /* unsigned dev builds cannot register a login item; the pane reads back the truth */
      }
    }
    if (next.appearance.reduceTransparency !== last?.appearance.reduceTransparency) {
      applyVibrancy(next.appearance.reduceTransparency)
    }
    if (next.appearance.uiScale !== last?.appearance.uiScale) {
      applyUiScale(next.appearance.uiScale)
    }
    last = next
  }
  apply(settings.get())
  settings.subscribe(apply)
}

/**
 * The gateway's WebSocket guard admits loopback Origins or no Origin at all.
 * A renderer loaded from disk sends `Origin: file://` (and `null` for fetch),
 * which it rejects with close code 1008. Present the gateway's own origin on
 * every loopback request instead: the renderer is the local operator, the
 * same trust the browser console gets when the gateway serves it.
 */
function installLoopbackOriginRewrite(): void {
  const urls = ['http://127.0.0.1/*', 'ws://127.0.0.1/*', 'http://localhost/*', 'ws://localhost/*']
  session.defaultSession.webRequest.onBeforeSendHeaders({ urls }, (details, callback) => {
    const requestHeaders = { ...details.requestHeaders }
    try {
      const target = new URL(details.url)
      const scheme = target.protocol === 'ws:' ? 'http:' : target.protocol
      requestHeaders.Origin = `${scheme}//${target.host}`
    } catch {
      /* leave headers untouched */
    }
    callback({ requestHeaders })
  })
}
