import { useSyncExternalStore } from 'react'
import { toast } from 'sonner'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { MAX_PENDING, enqueuePending, type PendingItem } from '@/views/chat/logic'

// Messages queued while a turn runs, per session, for the tab's life. They
// live outside the chat workspace, which remounts per session: a queue left
// in one session is still there when the user comes back to it.

const EMPTY: PendingItem[] = []
const queues = new Map<string, PendingItem[]>()
const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function readQueue(sessionKey: string): PendingItem[] {
  return queues.get(sessionKey) ?? EMPTY
}

export function writeQueue(sessionKey: string, items: PendingItem[]): void {
  if (items.length) queues.set(sessionKey, items)
  else queues.delete(sessionKey)
  for (const listener of [...listeners]) listener()
}

/** Queue a message behind the running turn; false (with a toast) when full. */
export function enqueueMessage(sessionKey: string, item: PendingItem): boolean {
  const result = enqueuePending(readQueue(sessionKey), item)
  if (!result.ok) {
    toast.warning(t('webChat.queueFull', { max: MAX_PENDING }), { duration: 3000 })
    return false
  }
  writeQueue(sessionKey, result.queue)
  toast.info(t('webChat.queuedToast', { count: result.queue.length, max: MAX_PENDING }), {
    duration: 1500,
  })
  return true
}

/** The session's queue, live. */
export function useQueue(sessionKey: string): PendingItem[] {
  return useSyncExternalStore(
    subscribe,
    () => readQueue(sessionKey),
    () => readQueue(sessionKey),
  )
}

/** Test seam: forget every queue. */
export function resetQueuesForTests(): void {
  queues.clear()
  for (const listener of [...listeners]) listener()
}
