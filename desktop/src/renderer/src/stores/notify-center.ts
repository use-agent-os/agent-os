import { create } from 'zustand'
import type { NotifyKind, NotifyTarget } from '@shared/notify'

export interface NotifyItem {
  id: string
  kind: NotifyKind
  title: string
  subtitle?: string
  body?: string
  target: NotifyTarget
  at: number
  seen: boolean
}

const MAX_ITEMS = 50

interface NotifyCenterStore {
  /** Newest first. */
  items: NotifyItem[]
  /** The bell's popover is showing. */
  open: boolean
  push(item: Omit<NotifyItem, 'id'>): NotifyItem
  markAllSeen(): void
  /** Everything pointing at this session was just looked at. */
  markSessionSeen(key: string): void
  dismiss(id: string): void
  clear(): void
  setOpen(open: boolean): void
}

let seq = 0

/**
 * The recent notifications, in memory for the life of the window. The bell
 * in the toolbar reads the unseen count; its popover lists them; the Dock
 * badge mirrors the count. Seen means the user had it on screen when it
 * happened, opened the popover since, or opened the session it points at.
 */
export const useNotifyCenter = create<NotifyCenterStore>((set) => ({
  items: [],
  open: false,
  push(item) {
    const full: NotifyItem = { ...item, id: `n${++seq}-${item.at}` }
    set((s) => ({ items: [full, ...s.items].slice(0, MAX_ITEMS) }))
    return full
  },
  markAllSeen() {
    set((s) =>
      s.items.some((i) => !i.seen) ? { items: s.items.map((i) => ({ ...i, seen: true })) } : s,
    )
  },
  markSessionSeen(key) {
    set((s) => {
      const hit = (i: NotifyItem) => !i.seen && i.target.type === 'session' && i.target.key === key
      if (!s.items.some(hit)) return s
      return { items: s.items.map((i) => (hit(i) ? { ...i, seen: true } : i)) }
    })
  },
  dismiss(id) {
    set((s) => ({ items: s.items.filter((i) => i.id !== id) }))
  },
  clear() {
    set({ items: [] })
  },
  setOpen(open) {
    set((s) => {
      if (!open) return { open }
      // Opening the popover is reading it.
      return { open, items: s.items.map((i) => (i.seen ? i : { ...i, seen: true })) }
    })
  },
}))

export function unseenCount(items: readonly NotifyItem[]): number {
  let n = 0
  for (const i of items) if (!i.seen) n += 1
  return n
}
