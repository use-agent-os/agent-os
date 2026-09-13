import { contextBridge, ipcRenderer } from 'electron'
import type { ChooseFileOptions } from '@shared/app'
import { IPC, type DesktopApi, type SettingsPatch } from '@shared/ipc'
import type { GatewayStatus } from '@shared/gateway'
import type { NotifyRequest, NotifyTarget, SystemSound } from '@shared/notify'
import type { ResolvedTheme, ThemeSettings } from '@shared/theme'
import type { AppUpdateState, EngineUpdateState } from '@shared/updates'
import type { BootstrapState } from '@shared/bootstrap'

/** Subscribe to a main -> renderer push channel and return an unsubscribe. */
function listen<T>(channel: string, listener: (payload: T) => void): () => void {
  const handler = (_e: Electron.IpcRendererEvent, payload: T) => listener(payload)
  ipcRenderer.on(channel, handler)
  return () => ipcRenderer.removeListener(channel, handler)
}

const api: DesktopApi = {
  app: {
    version: () => ipcRenderer.invoke(IPC.app.version),
    info: () => ipcRenderer.invoke(IPC.app.info),
    openExternal: (url: string) => ipcRenderer.invoke(IPC.app.openExternal, url),
    showItemInFolder: (path: string) => ipcRenderer.invoke(IPC.app.showItemInFolder, path),
    openPath: (path: string) => ipcRenderer.invoke(IPC.app.openPath, path),
    chooseFile: (options?: ChooseFileOptions) => ipcRenderer.invoke(IPC.app.chooseFile, options),
    loginItem: () => ipcRenderer.invoke(IPC.app.loginItem),
  },
  settings: {
    get: () => ipcRenderer.invoke(IPC.settings.get),
    update: (patch: SettingsPatch) => ipcRenderer.invoke(IPC.settings.update, patch),
    reset: () => ipcRenderer.invoke(IPC.settings.reset),
    onOpenRequested: (listener) => listen<void>(IPC.settings.open, () => listener()),
  },
  theme: {
    set: (next: Partial<ThemeSettings>) => ipcRenderer.invoke(IPC.theme.set, next),
    resolved: () => ipcRenderer.invoke(IPC.theme.resolved),
    onChanged: (listener) => listen<ResolvedTheme>(IPC.theme.changed, listener),
  },
  pets: {
    manifest: () => ipcRenderer.invoke(IPC.pets.manifest),
    installed: () => ipcRenderer.invoke(IPC.pets.installed),
    install: (slug: string) => ipcRenderer.invoke(IPC.pets.install, slug),
    remove: (slug: string) => ipcRenderer.invoke(IPC.pets.remove, slug),
    preview: (slug: string) => ipcRenderer.invoke(IPC.pets.preview, slug),
  },
  gateway: {
    status: () => ipcRenderer.invoke(IPC.gateway.status),
    start: () => ipcRenderer.invoke(IPC.gateway.start),
    stop: () => ipcRenderer.invoke(IPC.gateway.stop),
    restart: () => ipcRenderer.invoke(IPC.gateway.restart),
    onChanged: (listener) => listen<GatewayStatus>(IPC.gateway.changed, listener),
  },
  notify: {
    supported: () => ipcRenderer.invoke(IPC.notify.supported),
    show: (request: NotifyRequest) => ipcRenderer.invoke(IPC.notify.show, request),
    sound: (name: SystemSound) => ipcRenderer.invoke(IPC.notify.sound, name),
    badge: (count: number) => ipcRenderer.invoke(IPC.notify.badge, count),
    bounce: () => ipcRenderer.invoke(IPC.notify.bounce),
    openSystemSettings: () => ipcRenderer.invoke(IPC.notify.openSystemSettings),
    onActivated: (listener) => listen<NotifyTarget>(IPC.notify.activated, listener),
  },
  updates: {
    engine: {
      state: () => ipcRenderer.invoke(IPC.updates.engineState),
      check: () => ipcRenderer.invoke(IPC.updates.engineCheck),
      apply: () => ipcRenderer.invoke(IPC.updates.engineApply),
      onChanged: (listener) => listen<EngineUpdateState>(IPC.updates.engineChanged, listener),
    },
    app: {
      state: () => ipcRenderer.invoke(IPC.updates.appState),
      check: () => ipcRenderer.invoke(IPC.updates.appCheck),
      download: () => ipcRenderer.invoke(IPC.updates.appDownload),
      install: () => ipcRenderer.invoke(IPC.updates.appInstall),
      onChanged: (listener) => listen<AppUpdateState>(IPC.updates.appChanged, listener),
    },
  },
  bootstrap: {
    state: () => ipcRenderer.invoke(IPC.bootstrap.state),
    install: () => ipcRenderer.invoke(IPC.bootstrap.install),
    cancel: () => ipcRenderer.invoke(IPC.bootstrap.cancel),
    connectExisting: () => ipcRenderer.invoke(IPC.bootstrap.connectExisting),
    reinstall: () => ipcRenderer.invoke(IPC.bootstrap.reinstall),
    uninstallEngine: () => ipcRenderer.invoke(IPC.bootstrap.uninstallEngine),
    openLog: () => ipcRenderer.invoke(IPC.bootstrap.openLog),
    onChanged: (listener) => listen<BootstrapState>(IPC.bootstrap.changed, listener),
  },
}

contextBridge.exposeInMainWorld('agentos', api)
