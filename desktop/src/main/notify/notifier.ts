import { app, BrowserWindow, Notification, shell } from 'electron'
import { spawn } from 'node:child_process'
import { IPC } from '@shared/ipc'
import {
  isSystemSound,
  type NotifyRequest,
  type NotifyResult,
  type NotifyTarget,
  type SystemSound,
} from '@shared/notify'

/**
 * The native half of notifications. Main posts through Electron's
 * `Notification` (Notification Center on macOS) rather than the renderer's
 * web API: a click can then focus the window and hand the renderer a target
 * to open, and nothing depends on the renderer's permission state. The
 * decision to notify at all is the renderer's; this module only knows how.
 */

const SOUNDS_DIR = '/System/Library/Sounds'

/** macOS Focus / the user's System Settings still apply on top of this. */
export function notificationsSupported(): boolean {
  return Notification.isSupported()
}

export function showNotification(request: NotifyRequest): NotifyResult {
  if (!Notification.isSupported()) return { shown: false }
  try {
    const n = new Notification({
      title: request.title,
      subtitle: request.subtitle,
      body: request.body ?? '',
      // Sound is the renderer's job (playSound), so a chime and a system
      // notification never double up.
      silent: true,
    })
    const target = request.target
    n.on('click', () => activate(target))
    n.show()
    return { shown: true }
  } catch {
    return { shown: false }
  }
}

/** Bring the window forward and tell the renderer where the click points. */
function activate(target: NotifyTarget): void {
  const win = BrowserWindow.getAllWindows().find((w) => !w.isDestroyed())
  if (!win) {
    app.emit('activate')
    return
  }
  if (win.isMinimized()) win.restore()
  win.show()
  win.focus()
  win.webContents.send(IPC.notify.activated, target)
}

/**
 * Play a macOS alert sound. `afplay` ships with the OS; failures (no such
 * sound, not macOS) are swallowed because a missing sound is not an error
 * the user can act on.
 */
export function playSystemSound(name: SystemSound): void {
  if (process.platform !== 'darwin' || !isSystemSound(name)) return
  try {
    const child = spawn('afplay', [`${SOUNDS_DIR}/${name}.aiff`], {
      stdio: 'ignore',
      detached: false,
    })
    child.on('error', () => {})
  } catch {
    /* no afplay: silence */
  }
}

export function setDockBadge(count: number): void {
  const dock = app.dock
  if (!dock) return
  const n = Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0
  dock.setBadge(n > 0 ? String(n > 99 ? '99+' : n) : '')
}

/** One informational bounce. macOS ignores it while the app is frontmost. */
export function bounceDock(): void {
  app.dock?.bounce('informational')
}

/** The pane in System Settings where the user allows or silences the app. */
export function openNotificationSettings(): void {
  void shell.openExternal('x-apple.systempreferences:com.apple.Notifications-Settings.extension')
}
