import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'
import type { AppInfo, ChooseFileOptions } from '@shared/app'
import { IPC } from '@shared/ipc'
import type { SettingsStore } from '../settings/store'

/**
 * Shell-level facts and the few OS actions the Settings window needs:
 * versions and paths for About/Advanced, Finder reveals, the open-file sheet
 * for the CLI path, and the login-item state as macOS actually reports it.
 * Every handler validates its input: the renderer is sandboxed and these are
 * the only doors out of it.
 */
export function registerAppIpc(settings: SettingsStore): void {
  ipcMain.handle(IPC.app.version, () => app.getVersion())

  ipcMain.handle(IPC.app.info, (): AppInfo => ({
    version: app.getVersion(),
    electron: process.versions.electron ?? '',
    chrome: process.versions.chrome ?? '',
    node: process.versions.node ?? '',
    platform: process.platform,
    arch: process.arch,
    packaged: app.isPackaged,
    paths: {
      userData: app.getPath('userData'),
      settings: settings.filePath,
      logs: app.getPath('logs'),
    },
  }))

  ipcMain.handle(IPC.app.openExternal, async (_e, url: unknown) => {
    if (typeof url !== 'string') return
    let parsed: URL
    try {
      parsed = new URL(url)
    } catch {
      return
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return
    await shell.openExternal(parsed.toString())
  })

  ipcMain.handle(IPC.app.showItemInFolder, (_e, target: unknown) => {
    if (typeof target === 'string' && target) shell.showItemInFolder(target)
  })

  ipcMain.handle(IPC.app.openPath, async (_e, target: unknown): Promise<string> => {
    if (typeof target !== 'string' || !target) return 'No path given.'
    return shell.openPath(target)
  })

  ipcMain.handle(
    IPC.app.chooseFile,
    async (event, options: ChooseFileOptions = {}): Promise<string | null> => {
      const win = BrowserWindow.fromWebContents(event.sender)
      const properties: ('openFile' | 'openDirectory' | 'showHiddenFiles')[] =
        options.kind === 'directory' ? ['openDirectory'] : ['openFile', 'showHiddenFiles']
      const dialogOptions = {
        title: options.title,
        defaultPath: options.defaultPath,
        properties,
      }
      const result = win
        ? await dialog.showOpenDialog(win, dialogOptions)
        : await dialog.showOpenDialog(dialogOptions)
      if (result.canceled) return null
      return result.filePaths[0] ?? null
    },
  )

  ipcMain.handle(IPC.app.loginItem, () => app.getLoginItemSettings().openAtLogin)
}

/** Push "open settings" to the focused window (or the first one). */
export function requestOpenSettings(): void {
  const win = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0]
  if (win && !win.isDestroyed()) {
    if (win.isMinimized()) win.restore()
    win.focus()
    win.webContents.send(IPC.settings.open)
  }
}
