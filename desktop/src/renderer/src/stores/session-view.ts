import { create } from 'zustand'
import {
  DEFAULT_VIEW,
  GROUPINGS,
  ORDERINGS,
  STATUS_FILTERS,
  type SessionView,
} from '~/components/sidebar/logic'

const KEY = 'agentos-desktop.sessionView'

function load(): SessionView {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '{}') as Partial<SessionView>
    const pick = <T>(value: unknown, allowed: readonly T[], fallback: T): T =>
      allowed.includes(value as T) ? (value as T) : fallback
    const bool = (value: unknown, fallback: boolean) =>
      typeof value === 'boolean' ? value : fallback
    const str = (value: unknown) => (typeof value === 'string' ? value : null)
    return {
      grouping: pick(raw.grouping, GROUPINGS, DEFAULT_VIEW.grouping),
      ordering: pick(raw.ordering, ORDERINGS, DEFAULT_VIEW.ordering),
      folders: bool(raw.folders, DEFAULT_VIEW.folders),
      ages: bool(raw.ages, DEFAULT_VIEW.ages),
      inbox: bool(raw.inbox, DEFAULT_VIEW.inbox),
      status: pick(raw.status, STATUS_FILTERS, DEFAULT_VIEW.status),
      agent: str(raw.agent),
      project: str(raw.project),
      archived: bool(raw.archived, DEFAULT_VIEW.archived),
    }
  } catch {
    return { ...DEFAULT_VIEW }
  }
}

function save(view: SessionView): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(view))
  } catch {
    /* storage unavailable */
  }
}

interface SessionViewStore {
  view: SessionView
  set(patch: Partial<SessionView>): void
  reset(): void
}

/** The sidebar's view menu choices, remembered between launches. */
export const useSessionView = create<SessionViewStore>((set, get) => ({
  view: load(),
  set: (patch) => {
    const view = { ...get().view, ...patch }
    save(view)
    set({ view })
  },
  reset: () => {
    save(DEFAULT_VIEW)
    set({ view: { ...DEFAULT_VIEW } })
  },
}))
