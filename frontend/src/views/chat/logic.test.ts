import { describe, it, expect } from 'vitest'
import {
  agentIdFromSessionKey,
  canonicalSessionKey,
  historyFallbackMessageIdentity,
  historyStableMessageIdentity,
  messageTranscriptId,
  readSessionFromUrl,
  webchatSessionKey,
} from './logic'
import type { ChatMessage } from './types'

// Parity: chat.js:1145-1149 — the agent id is segment [1] of an `agent:` key,
// normalized (chat.js:1138-1143); anything else (or a non-agent key) is 'main'.
describe('agentIdFromSessionKey', () => {
  it('extracts the agent id from a webchat session key', () => {
    expect(agentIdFromSessionKey('agent:main:webchat:default')).toBe('main')
  })
  it('extracts a non-default agent id', () => {
    expect(agentIdFromSessionKey('agent:trader:webchat:default')).toBe('trader')
  })
  it('normalizes an uppercase / punctuated agent id (chat.js:1138-1143)', () => {
    expect(agentIdFromSessionKey('agent:My Bot!:webchat:default')).toBe('my-bot')
  })
  it('maps a non-agent key to main', () => {
    expect(agentIdFromSessionKey('sess-123')).toBe('main')
  })
  it('maps the literal default segment to main (chat.js:1140)', () => {
    expect(agentIdFromSessionKey('agent:default:webchat:default')).toBe('main')
  })
  it('maps empty / nullish input to main', () => {
    expect(agentIdFromSessionKey('')).toBe('main')
  })
})

// Parity: chat.js:1151-1153.
describe('webchatSessionKey', () => {
  it('builds a webchat session key with the default suffix', () => {
    expect(webchatSessionKey('main')).toBe('agent:main:webchat:default')
  })
  it('normalizes the agent id and honors a custom suffix', () => {
    expect(webchatSessionKey('Trader', 'abc123')).toBe('agent:trader:webchat:abc123')
  })
})

// Parity: chat.js:1159-1165. The stable key is 'agent:main:webchat:default'.
describe('canonicalSessionKey', () => {
  it('canonicalizes the empty / default aliases to the stable key', () => {
    expect(canonicalSessionKey('')).toBe('agent:main:webchat:default')
    expect(canonicalSessionKey('default')).toBe('agent:main:webchat:default')
    expect(canonicalSessionKey('webchat:default')).toBe('agent:main:webchat:default')
    expect(canonicalSessionKey('   ')).toBe('agent:main:webchat:default')
  })
  it('rewrites an agent:default: prefix to agent:main: (chat.js:1162)', () => {
    expect(canonicalSessionKey('agent:default:webchat:default')).toBe('agent:main:webchat:default')
  })
  it('rewrites a sess- prefix to an agent:main webchat key (chat.js:1163)', () => {
    expect(canonicalSessionKey('sess-abc')).toBe('agent:main:webchat:abc')
  })
  it('passes an already-canonical key through unchanged', () => {
    expect(canonicalSessionKey('agent:trader:webchat:default')).toBe('agent:trader:webchat:default')
  })
})

// Parity: chat.js:1182-1187 — read ?session= from the search string, else ''.
// Ported pure over an injected search string; returns null when absent.
describe('readSessionFromUrl', () => {
  it('reads ?session= from a search string, else null', () => {
    expect(readSessionFromUrl('?session=agent%3Amain%3Awebchat%3Adefault')).toBe(
      'agent:main:webchat:default',
    )
    expect(readSessionFromUrl('')).toBeNull()
  })
  it('returns null when session is absent but other params exist', () => {
    expect(readSessionFromUrl('?agent=main')).toBeNull()
  })
})

// Parity: chat.js:3086-3090 — transcript_id parsed as a finite number, else null.
describe('messageTranscriptId', () => {
  it('returns a finite numeric transcript id as a string', () => {
    const msg = { role: 'assistant', text: 'hi', transcript_id: 42 } as unknown as ChatMessage
    expect(messageTranscriptId(msg)).toBe('42')
  })
  it('returns null for a non-numeric / missing transcript id', () => {
    expect(messageTranscriptId({ role: 'user', text: 'hi' })).toBeNull()
    const bad = { role: 'user', text: 'hi', transcript_id: 'nope' } as unknown as ChatMessage
    expect(messageTranscriptId(bad)).toBeNull()
  })
})

// Parity: chat.js:5833-5836 — message_id || id, stringified; '' when neither.
describe('historyStableMessageIdentity', () => {
  it('prefers message_id, then id, stringified', () => {
    const m1 = { role: 'user', text: 'x', message_id: 7 } as unknown as ChatMessage
    expect(historyStableMessageIdentity(m1)).toBe('7')
    const m2 = { role: 'user', text: 'x', id: 'abc' } as unknown as ChatMessage
    expect(historyStableMessageIdentity(m2)).toBe('abc')
  })
  it('returns an empty string when there is no stable id', () => {
    expect(historyStableMessageIdentity({ role: 'user', text: 'x' })).toBe('')
  })
})

// Parity: chat.js:5838-5839 — `${role}|${text}` (the fallback text pipeline is
// ported by later tasks; this task passes the text through trimmed).
describe('historyFallbackMessageIdentity', () => {
  it('joins role and text with a pipe', () => {
    expect(historyFallbackMessageIdentity('user', 'hello')).toBe('user|hello')
  })
  it('trims the text', () => {
    expect(historyFallbackMessageIdentity('assistant', '  hi  ')).toBe('assistant|hi')
  })
})
