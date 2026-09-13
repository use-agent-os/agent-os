import { ipcMain } from 'electron'
import { IPC } from '@shared/ipc'
import type { PetStore } from '../pets/store'

export function registerPetsIpc(pets: PetStore): void {
  ipcMain.handle(IPC.pets.manifest, () => pets.fetchManifest())
  ipcMain.handle(IPC.pets.installed, () => pets.installed())
  ipcMain.handle(IPC.pets.install, (_e, slug: string) => pets.install(String(slug)))
  ipcMain.handle(IPC.pets.remove, (_e, slug: string) => pets.remove(String(slug)))
  ipcMain.handle(IPC.pets.preview, (_e, slug: string) => pets.preview(String(slug)))
}
