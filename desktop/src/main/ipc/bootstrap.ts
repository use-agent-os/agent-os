import { BrowserWindow, ipcMain, shell } from 'electron'
import { IPC } from '@shared/ipc'
import type { BootstrapController } from '../bootstrap/controller'
import type { BootstrapRunner } from '../bootstrap/runner'

export function registerBootstrapIpc(
  runner: BootstrapRunner,
  controller: BootstrapController,
): void {
  ipcMain.handle(IPC.bootstrap.state, () => runner.current())
  ipcMain.handle(IPC.bootstrap.install, () => controller.install())
  ipcMain.handle(IPC.bootstrap.cancel, () => runner.cancel())
  ipcMain.handle(IPC.bootstrap.connectExisting, () => controller.connectExisting())
  ipcMain.handle(IPC.bootstrap.reinstall, () => controller.reinstall())
  ipcMain.handle(IPC.bootstrap.uninstallEngine, () => controller.uninstallEngine())
  ipcMain.handle(IPC.bootstrap.openLog, async () => {
    const file = runner.current().logPath
    if (file) shell.showItemInFolder(file)
  })

  runner.subscribe((state) => {
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) win.webContents.send(IPC.bootstrap.changed, state)
    }
  })
}
