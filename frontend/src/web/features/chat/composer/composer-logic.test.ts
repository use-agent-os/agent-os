import { beforeEach, describe, expect, it } from 'vitest'
import {
  DRAFTS_KEY,
  MAX_DRAFT_CHARS,
  MAX_DRAFT_SESSIONS,
  clearDraft,
  loadDraft,
  loadStagedAttachments,
  saveDraft,
  saveStagedAttachments,
} from './drafts'
import { composerEnterAction, type ComposerKey } from './keys'

const key = (patch: Partial<ComposerKey> = {}): ComposerKey => ({
  key: 'Enter',
  shiftKey: false,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  ...patch,
})

describe('composerEnterAction', () => {
  it('Enter mode: Enter sends, Shift+Enter is a new line', () => {
    expect(composerEnterAction(key(), 'enter', false)).toBe('send')
    expect(composerEnterAction(key({ shiftKey: true }), 'enter', false)).toBe('newline')
  })

  it('Mod+Enter mode: Enter is a new line, Ctrl+Enter or Cmd+Enter sends', () => {
    expect(composerEnterAction(key(), 'mod-enter', false)).toBe('newline')
    expect(composerEnterAction(key({ ctrlKey: true }), 'mod-enter', false)).toBe('send')
    expect(composerEnterAction(key({ metaKey: true }), 'mod-enter', false)).toBe('send')
  })

  it('never sends on an Enter that confirms an IME composition', () => {
    expect(composerEnterAction(key({ isComposing: true }), 'enter', false)).toBeNull()
    expect(composerEnterAction(key({ keyCode: 229 }), 'enter', false)).toBeNull()
  })

  it('treats Return as a new line on touch devices', () => {
    expect(composerEnterAction(key(), 'enter', true)).toBe('newline')
  })

  it('ignores other keys', () => {
    expect(composerEnterAction(key({ key: 'a' }), 'enter', false)).toBeNull()
  })
})

describe('drafts', () => {
  beforeEach(() => localStorage.clear())

  it('keeps one draft per session and clears it once sent', () => {
    saveDraft('A', 'draft for A')
    saveDraft('B', 'draft for B')
    expect(loadDraft('A')).toBe('draft for A')
    expect(loadDraft('B')).toBe('draft for B')
    clearDraft('A')
    expect(loadDraft('A')).toBe('')
    expect(loadDraft('B')).toBe('draft for B')
  })

  it('caps the number of sessions, dropping the oldest', () => {
    for (let i = 0; i < MAX_DRAFT_SESSIONS + 5; i++) saveDraft(`s${i}`, 'x', i)
    const stored = JSON.parse(localStorage.getItem(DRAFTS_KEY)!)
    expect(Object.keys(stored)).toHaveLength(MAX_DRAFT_SESSIONS)
    expect(loadDraft('s0')).toBe('')
    expect(loadDraft(`s${MAX_DRAFT_SESSIONS + 4}`)).toBe('x')
  })

  it('caps the length of each draft', () => {
    saveDraft('A', 'y'.repeat(MAX_DRAFT_CHARS + 10))
    expect(loadDraft('A')).toHaveLength(MAX_DRAFT_CHARS)
  })

  it('keeps staged attachments per session in memory', () => {
    const att = { kind: 'inline' as const, local_id: 1, name: 'a.png', mime: 'image/png', size: 3 }
    saveStagedAttachments('A', [att])
    expect(loadStagedAttachments('A')).toEqual([att])
    expect(loadStagedAttachments('B')).toEqual([])
    saveStagedAttachments('A', [])
    expect(loadStagedAttachments('A')).toEqual([])
  })
})
