import { ipcMain } from 'electron'
import { IPC, type SettingsPatch } from '@shared/ipc'
import type { SettingsStore } from '../settings/store'

export function registerSettingsIpc(settings: SettingsStore): void {
  ipcMain.handle(IPC.settings.get, () => settings.get())
  ipcMain.handle(IPC.settings.update, (_e, patch: SettingsPatch) => settings.update(patch))
  ipcMain.handle(IPC.settings.reset, () => settings.reset())
}
