import { app, BrowserWindow, globalShortcut } from 'electron'
import { electronApp, optimizer } from '@electron-toolkit/utils'
import { existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import { gatewayUrl, type DesktopSettings } from '@shared/settings'
import { GatewaySupervisor } from './gateway/supervisor'
import { registerIpc } from './ipc'
import { gatewayOriginFor, installGatewayCors } from './loopback-cors'
import { installAppMenu } from './menu'
import { registerPetScheme, servePets } from './pets/protocol'
import { QuickAskController } from './quick-ask/controller'
import { QuickAskPanel } from './quick-ask/panel'
import { bundledPetsDir, PetStore } from './pets/store'
import { BootstrapController } from './bootstrap/controller'
import { BootstrapRunner, bundledInstallScript } from './bootstrap/runner'
import { SettingsStore } from './settings/store'
import { MenuBar } from './tray/tray'
import { appCalver } from './app-version'
import { AppUpdateController, type UpdaterLike } from './updates/app-updater'
import { startAutoCheck } from './updates/auto-check'
import { defaultMarkerPath, EngineUpdater } from './updates/engine-updater'
import {
  applyUiScale,
  applyVibrancy,
  createMainWindow,
  findMainWindow,
  rendererAppOrigin,
} from './window'

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
  // First-run engine install: install.sh is bundled next to the asar; in
  // development the repo's copy two levels up is used.
  const agentosHome = path.dirname(defaultMarkerPath()).replace(/\/state\/desktop$/, '')
  const bootstrapRunner = new BootstrapRunner({
    scriptPath:
      bundledInstallScript(process.resourcesPath, path.resolve(__dirname, '../../..')) ??
      path.join(process.resourcesPath, 'install.sh'),
    version: appCalver(),
    logDir: path.join(agentosHome, 'logs'),
    cwd: app.getPath('home'),
  })
  const appUpdater = new AppUpdateController({
    updater: loadAutoUpdater(),
    version: appCalver(),
    beforeInstall: stopGatewayForQuit,
    // A relaunch kills whichever installer is mid-write; refuse until it is done.
    installGate: () => {
      const engine = engineUpdater.current().phase
      if (engine === 'installing' || engine === 'restarting') return 'engine-updating'
      if (bootstrapRunner.current().phase === 'running') return 'installer-running'
      return null
    },
  })
  const bootstrap = new BootstrapController({
    runner: bootstrapRunner,
    gateway,
    getSettings: () => settings.get().gateway,
    updateSettings: (patch) => settings.update(patch),
    appVersion: appCalver(),
  })
  // Custom schemes must be declared before the app is ready.
  registerPetScheme()

  app.on('second-instance', () => {
    const win = findMainWindow()
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

    installGatewayCors(rendererAppOrigin(), () =>
      gatewayOriginFor(gatewayUrl(settings.get().gateway)),
    )
    servePets(pets)
    // The pets that ship with the app, so Settings > Appearance has one to
    // offer before anyone reaches petdex.dev. Seeded once each; never fatal.
    const bundledPets = bundledPetsDir(process.resourcesPath, path.resolve(__dirname, '../..'))
    if (bundledPets) void pets.seedBundled(bundledPets).catch(() => {})
    const quickAsk = new QuickAskController({
      registry: globalShortcut,
      panel: new QuickAskPanel({
        reduceTransparency: () => settings.get().appearance.reduceTransparency,
      }),
      findMainWindow,
      openMainWindow: () => createMainWindow(windowOptions(settings.get())),
      focusApp: () => app.focus({ steal: true }),
      broadcast: (channel, payload) => {
        for (const win of BrowserWindow.getAllWindows()) {
          if (!win.isDestroyed()) win.webContents.send(channel, payload)
        }
      },
    })
    app.once('will-quit', () => {
      quickAsk.dispose()
      globalShortcut.unregisterAll()
    })
    // The menu bar item; `mirrorSettingsToOs` creates it (or not) from the setting.
    const menuBar = new MenuBar({
      settings,
      gateway,
      iconPath: trayIconPath(),
      createWindow: () => createMainWindow(windowOptions(settings.get())),
      showQuickAsk: () => quickAsk.show(),
    })
    registerIpc({
      settings,
      gateway,
      pets,
      engineUpdater,
      appUpdater,
      bootstrapRunner,
      bootstrap,
      menuBar,
      quickAsk,
    })
    installAppMenu(settings)
    createMainWindow(windowOptions(settings.get()))
    mirrorSettingsToOs(settings, gateway, menuBar, quickAsk)
    // The shell is only useful with a gateway behind it. The controller
    // finds the engine and starts the gateway, or offers to install the
    // engine first when this Mac has none (or an older one).
    void bootstrap.launch()
    // A marker from a previous launch means an engine update never finished.
    engineUpdater.recover()
    // Ambient checks for a new release, app and engine alike: after launch,
    // on focus, every 5 minutes. Silent by contract; a hit surfaces as the
    // shell's single "AgentOS X is available" notice.
    const stopAutoCheck = startAutoCheck({
      check: () =>
        Promise.all([appUpdater.check({ silent: true }), engineUpdater.check({ silent: true })]),
      onFocus: (listener) => {
        app.on('browser-window-focus', listener)
        return () => app.removeListener('browser-window-focus', listener)
      },
    })
    app.once('will-quit', stopAutoCheck)

    // The Quick Ask panel is a window too, so "no windows" means no main window.
    app.on('activate', () => {
      if (!findMainWindow()) createMainWindow(windowOptions(settings.get()))
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

/**
 * The menu bar icon: a monochrome template (`trayTemplate.png` + `@2x`) that
 * macOS tints for a light or dark bar. Packaged, electron-builder copies the
 * pair next to the asar (`extraResources`); in development it is read from
 * the repo, as the Dock icon is.
 */
function trayIconPath(): string {
  return app.isPackaged
    ? path.join(process.resourcesPath, 'trayTemplate.png')
    : path.resolve(__dirname, '../../resources/trayTemplate.png')
}

function windowOptions(s: DesktopSettings): { reduceTransparency: boolean; uiScale: number } {
  return { reduceTransparency: s.appearance.reduceTransparency, uiScale: s.appearance.uiScale }
}

/**
 * Some settings are really window/OS state: the login item, window
 * vibrancy, the zoom factor, the menu bar item and the Quick Ask global
 * shortcut. Apply them at boot and again on every change so the file and
 * what is on screen agree.
 */
function mirrorSettingsToOs(
  settings: SettingsStore,
  gateway: GatewaySupervisor,
  menuBar: MenuBar,
  quickAsk: QuickAskController,
): void {
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
    // Run mode changed: external stops the gateway the app spawned instead of
    // leaving it behind; managed starts (or adopts) the local one. See
    // GatewaySupervisor.modeChanged.
    if (last && next.gateway.mode !== last.gateway.mode) {
      void gateway.modeChanged(last.gateway.mode, next.gateway.mode)
    }
    // Every write: the menu shows the gateway mode and endpoint, not just the toggle.
    menuBar.sync(next)
    // Every write too: `apply` is a no-op while the wanted key is held, and
    // a key macOS refused is retried, since the app holding it may have let go.
    quickAsk.apply(next.quickAsk)
    last = next
  }
  apply(settings.get())
  settings.subscribe(apply)
}
