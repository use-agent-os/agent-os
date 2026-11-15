import { app, BrowserWindow, clipboard, dialog, ipcMain, shell } from 'electron'
import type { AppInfo, BiometricsInfo, ChooseFileOptions } from '@shared/app'
import { IPC } from '@shared/ipc'
import { appCalver } from '../app-version'
import { ClipboardScrub, scrubBeforeQuit } from '../clipboard-scrub'
import { authenticate, biometricsAvailable } from '../security'
import { findMainWindow } from '../window'
import type { SettingsStore } from '../settings/store'

/**
 * Shell-level facts and the few OS actions the Settings window needs:
 * versions and paths for About/Advanced, Finder reveals, the open-file sheet
 * for the CLI path, and the login-item state as macOS actually reports it.
 * Every handler validates its input: the renderer is sandboxed and these are
 * the only doors out of it.
 */
export function registerAppIpc(settings: SettingsStore): void {
  ipcMain.handle(IPC.app.version, () => appCalver())

  ipcMain.handle(IPC.app.info, (): AppInfo => ({
    version: appCalver(),
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

  // Touch ID (Settings › Security). `authenticate` validates the reason itself:
  // anything but a short non-blank string is a failure, never a prompt.
  ipcMain.handle(IPC.app.biometrics, (): BiometricsInfo => ({ available: biometricsAvailable() }))
  ipcMain.handle(IPC.app.authenticate, (_e, reason: unknown) => authenticate(reason))

  // A copied private key leaves the clipboard on a timer main owns, so
  // closing the Export sheet (or reloading the window) cannot cancel it,
  // and quitting scrubs it at once. Only the secret's digest crosses IPC.
  const scrub = new ClipboardScrub({
    readText: () => clipboard.readText(),
    clear: () => clipboard.clear(),
    setTimer: (fn, ms) => setTimeout(fn, ms),
    clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  })
  ipcMain.handle(IPC.app.scrubClipboard, (_e, digest: unknown, afterMs: unknown) =>
    scrub.schedule(digest, afterMs),
  )
  // Quitting with a key still on the clipboard blanks it first.
  app.on('will-quit', (event) => scrubBeforeQuit(scrub, event, () => app.quit()))
}

/** Push "open settings" to the main window (never the Quick Ask panel). */
export function requestOpenSettings(): void {
  const win = findMainWindow()
  if (win && !win.isDestroyed()) {
    if (win.isMinimized()) win.restore()
    win.focus()
    win.webContents.send(IPC.settings.open)
  }
}
