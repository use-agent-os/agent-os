import { ipcMain } from 'electron'
import { IPC } from '@shared/ipc'
import type { NotifyRequest, NotifyResult, NotifyTarget } from '@shared/notify'
import { isSystemSound } from '@shared/notify'
import {
  bounceDock,
  notificationsSupported,
  openNotificationSettings,
  playSystemSound,
  setDockBadge,
  showNotification,
} from '../notify/notifier'

const KINDS = new Set(['reply', 'replyFailed', 'approval', 'job', 'jobFailed', 'gateway', 'test'])

function text(value: unknown, max: number): string {
  return typeof value === 'string' ? value.slice(0, max) : ''
}

/** Only the shapes the renderer is allowed to point a click at. */
function sanitizeTarget(raw: unknown): NotifyTarget {
  const obj = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  switch (obj.type) {
    case 'session': {
      const key = text(obj.key, 512)
      return key ? { type: 'session', key } : { type: 'none' }
    }
    case 'jobs': {
      const jobId = text(obj.jobId, 128)
      return jobId ? { type: 'jobs', jobId } : { type: 'jobs' }
    }
    case 'approvals':
      return { type: 'approvals' }
    case 'settings':
      return { type: 'settings' }
    default:
      return { type: 'none' }
  }
}

function sanitizeRequest(raw: unknown): NotifyRequest | null {
  const obj = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : null
  if (!obj) return null
  const title = text(obj.title, 200).trim()
  if (!title) return null
  const kind = typeof obj.kind === 'string' && KINDS.has(obj.kind) ? obj.kind : 'test'
  return {
    tag: text(obj.tag, 200) || kind,
    kind: kind as NotifyRequest['kind'],
    title,
    subtitle: text(obj.subtitle, 200) || undefined,
    body: text(obj.body, 1000) || undefined,
    target: sanitizeTarget(obj.target),
  }
}

/** The renderer is sandboxed: every payload is validated before it touches the OS. */
export function registerNotifyIpc(): void {
  ipcMain.handle(IPC.notify.supported, () => notificationsSupported())
  ipcMain.handle(IPC.notify.show, (_e, raw: unknown): NotifyResult => {
    const request = sanitizeRequest(raw)
    return request ? showNotification(request) : { shown: false }
  })
  ipcMain.handle(IPC.notify.sound, (_e, name: unknown) => {
    if (isSystemSound(name)) playSystemSound(name)
  })
  ipcMain.handle(IPC.notify.badge, (_e, count: unknown) => {
    setDockBadge(typeof count === 'number' ? count : 0)
  })
  ipcMain.handle(IPC.notify.bounce, () => bounceDock())
  ipcMain.handle(IPC.notify.openSystemSettings, () => openNotificationSettings())
}
