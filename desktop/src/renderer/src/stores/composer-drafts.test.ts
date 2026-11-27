import { beforeEach, describe, expect, it } from 'vitest'
import { MAX_DRAFTS, NEW_SESSION_DRAFT, readDraft, useComposerDrafts } from './composer-drafts'

const KEY = 'agentos-desktop.composerDrafts'
const stored = () => JSON.parse(localStorage.getItem(KEY) || '{}') as Record<string, string>

describe('useComposerDrafts', () => {
  beforeEach(() => {
    localStorage.clear()
    useComposerDrafts.setState({ drafts: {} })
  })

  it('keeps a draft per slot and persists it', () => {
    const s = useComposerDrafts.getState()
    s.setDraft(NEW_SESSION_DRAFT, 'half a thought')
    s.setDraft('agent:main:webchat:a', 'another')
    expect(readDraft(NEW_SESSION_DRAFT)).toBe('half a thought')
    expect(readDraft('agent:main:webchat:a')).toBe('another')
    expect(readDraft('agent:main:webchat:b')).toBe('')
    expect(stored()).toEqual({ new: 'half a thought', 'agent:main:webchat:a': 'another' })
  })

  it('drops the slot when the text is emptied or the session is forgotten', () => {
    const s = useComposerDrafts.getState()
    s.setDraft('a', 'x')
    s.setDraft('b', 'y')
    s.setDraft('a', '')
    s.forget('b')
    expect(useComposerDrafts.getState().drafts).toEqual({})
    expect(stored()).toEqual({})
  })

  it('moves a draft to another slot', () => {
    const s = useComposerDrafts.getState()
    s.setDraft(NEW_SESSION_DRAFT, 'mine')
    s.moveDraft(NEW_SESSION_DRAFT, 'agent:main:webchat:a')
    expect(useComposerDrafts.getState().drafts).toEqual({ 'agent:main:webchat:a': 'mine' })
  })

  it('keeps only the most recently edited drafts', () => {
    const s = useComposerDrafts.getState()
    for (let i = 0; i < MAX_DRAFTS; i++) s.setDraft(`k${i}`, 'x')
    s.setDraft('k0', 'edited again')
    s.setDraft('one-more', 'x')
    const { drafts } = useComposerDrafts.getState()
    expect(Object.keys(drafts)).toHaveLength(MAX_DRAFTS)
    expect(drafts.k0).toBe('edited again')
    expect(drafts.k1).toBeUndefined()
    expect(drafts['one-more']).toBe('x')
  })
})
