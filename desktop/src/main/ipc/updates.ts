import { BrowserWindow, ipcMain } from 'electron'
import { IPC } from '@shared/ipc'
import type { AppUpdateController } from '../updates/app-updater'
import type { EngineUpdater } from '../updates/engine-updater'

export function registerUpdatesIpc(engine: EngineUpdater, app: AppUpdateController): void {
  ipcMain.handle(IPC.updates.engineState, () => engine.current())
  ipcMain.handle(IPC.updates.engineCheck, () => engine.check())
  ipcMain.handle(IPC.updates.engineApply, () => engine.apply())
  ipcMain.handle(IPC.updates.appState, () => app.current())
  ipcMain.handle(IPC.updates.appCheck, () => app.check())
  ipcMain.handle(IPC.updates.appDownload, () => app.download())
  ipcMain.handle(IPC.updates.appInstall, () => app.install())

  const broadcast = (channel: string, payload: unknown) => {
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) win.webContents.send(channel, payload)
    }
  }
  engine.subscribe((state) => broadcast(IPC.updates.engineChanged, state))
  app.subscribe((state) => broadcast(IPC.updates.appChanged, state))
}
