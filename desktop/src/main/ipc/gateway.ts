import { BrowserWindow, ipcMain } from 'electron'
import { IPC } from '@shared/ipc'
import type { GatewaySupervisor } from '../gateway/supervisor'

export function registerGatewayIpc(supervisor: GatewaySupervisor): void {
  ipcMain.handle(IPC.gateway.status, () => supervisor.current())
  ipcMain.handle(IPC.gateway.start, () => supervisor.start())
  ipcMain.handle(IPC.gateway.stop, () => supervisor.stop())
  ipcMain.handle(IPC.gateway.restart, () => supervisor.restart())

  supervisor.subscribe((status) => {
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) win.webContents.send(IPC.gateway.changed, status)
    }
  })
}
