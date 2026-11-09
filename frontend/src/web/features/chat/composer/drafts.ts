import type { PendingAttachment } from '@/views/chat/logic'

// Per-session drafts (overhaul-control-ui D7, chat spec "Drafts are kept per
// session"). Text lives in localStorage — the 50 most recently edited sessions,
// 20k characters each; staged attachments live in memory for the tab's life.

export const DRAFTS_KEY = 'agentos.ui.drafts.v1'
export const MAX_DRAFT_SESSIONS = 50
export const MAX_DRAFT_CHARS = 20_000

interface StoredDraft {
  text: string
  at: number
}

type DraftMap = Record<string, StoredDraft>

function read(): DraftMap {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(DRAFTS_KEY) || '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const out: DraftMap = {}
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      const draft = value as Partial<StoredDraft> | null
      if (draft && typeof draft.text === 'string') {
        out[key] = { text: draft.text, at: typeof draft.at === 'number' ? draft.at : 0 }
      }
    }
    return out
  } catch {
    return {}
  }
}

function write(map: DraftMap): void {
  try {
    localStorage.setItem(DRAFTS_KEY, JSON.stringify(map))
  } catch {
    /* storage full or blocked: the draft survives in the composer only */
  }
}

export function loadDraft(sessionKey: string): string {
  return read()[sessionKey]?.text ?? ''
}

/** Save (or with empty text, clear) a session's draft, keeping the cap. */
export function saveDraft(sessionKey: string, text: string, now: number = Date.now()): void {
  const map = read()
  if (!text) {
    if (!(sessionKey in map)) return
    delete map[sessionKey]
    write(map)
    return
  }
  map[sessionKey] = { text: text.slice(0, MAX_DRAFT_CHARS), at: now }
  const keys = Object.keys(map)
  if (keys.length > MAX_DRAFT_SESSIONS) {
    keys
      .sort((a, b) => map[a]!.at - map[b]!.at)
      .slice(0, keys.length - MAX_DRAFT_SESSIONS)
      .forEach((k) => delete map[k])
  }
  write(map)
}

export function clearDraft(sessionKey: string): void {
  saveDraft(sessionKey, '')
}

const stagedAttachments = new Map<string, PendingAttachment[]>()

export function loadStagedAttachments(sessionKey: string): PendingAttachment[] {
  return stagedAttachments.get(sessionKey) ?? []
}

export function saveStagedAttachments(sessionKey: string, attachments: PendingAttachment[]): void {
  if (attachments.length === 0) stagedAttachments.delete(sessionKey)
  else
    stagedAttachments.set(
      sessionKey,
      attachments.map((a) => ({ ...a })),
    )
}

/** Test seam: forget the in-memory staged attachments. */
export function resetStagedAttachmentsForTests(): void {
  stagedAttachments.clear()
}
