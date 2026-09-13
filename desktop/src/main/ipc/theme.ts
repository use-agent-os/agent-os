import { BrowserWindow, ipcMain, nativeTheme } from 'electron'
import { IPC } from '@shared/ipc'
import { normalizeThemeSettings, type ResolvedTheme, type ThemeSettings } from '@shared/theme'
import type { SettingsStore } from '../settings/store'
import { applyPageBackground } from '../window'

/**
 * Theme bridge. The preference lives in settings; main mirrors it onto
 * `nativeTheme.themeSource` so window chrome, vibrancy and the renderer's
 * `prefers-color-scheme` all agree. When macOS flips appearance under a
 * `system` preference, every window is told the new resolved value.
 */
export function registerThemeIpc(settings: SettingsStore): void {
  applyThemeSource(settings.get().theme)
  // Any write to settings (a reset, not just theme:set) keeps the OS in step.
  settings.subscribe((s) => applyThemeSource(s.theme))

  ipcMain.handle(IPC.theme.set, (_e, next: Partial<ThemeSettings>): ThemeSettings => {
    const patch = normalizeThemeSettings({ ...settings.get().theme, ...next })
    const saved = settings.update({ theme: patch }).theme
    applyThemeSource(saved)
    return saved
  })

  ipcMain.handle(IPC.theme.resolved, (): ResolvedTheme => currentResolved())

  nativeTheme.on('updated', () => {
    const resolved = currentResolved()
    // Only matters with vibrancy off (the page is transparent otherwise), but
    // it is cheap and keeps the opaque ground in step with the appearance.
    applyPageBackground(settings.get().appearance.reduceTransparency)
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) win.webContents.send(IPC.theme.changed, resolved)
    }
  })
}

export function currentResolved(): ResolvedTheme {
  return nativeTheme.shouldUseDarkColors ? 'dark' : 'light'
}

function applyThemeSource(theme: ThemeSettings): void {
  if (nativeTheme.themeSource !== theme.preference) nativeTheme.themeSource = theme.preference
}
