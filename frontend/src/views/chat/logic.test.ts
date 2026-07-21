import { describe, it, expect } from 'vitest'
import {
  agentIdFromSessionKey,
  attachmentDownloadHref,
  attachmentDownloadName,
  attachmentHardCapBytes,
  canonicalSessionKey,
  canStageAttachmentMime,
  estimateTextTokens,
  hasPendingAttachmentWork,
  historyFallbackMessageIdentity,
  historyStableMessageIdentity,
  isAllowedAttachmentMime,
  isImageAttachmentMime,
  isTextAttachmentMime,
  ATTACHMENT_ALLOWED_LABEL,
  ATTACHMENT_IMAGE_HARD_CAP_BYTES,
  ATTACHMENT_PDF_HARD_CAP_BYTES,
  ATTACHMENT_TEXT_HARD_CAP_BYTES,
  LARGE_PASTE_CHARS,
  PAGE_DUMP_CHARS,
  messageTranscriptId,
  normalizeOutgoingComposerPayload,
  pageDumpMarkerScore,
  readSessionFromUrl,
  resolveAttachmentMime,
  sendButtonState,
  shouldAutofocusComposer,
  webchatSessionKey,
  type PendingAttachment,
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

// Parity: chat.js:1353-1360 `_shouldAutofocusComposer` — autofocus unless the
// viewport is narrow (max-width:768px) OR the pointer is coarse (touch).
// `matchMedia` is injected as an env probe so the pure helper is testable
// without a real `window` (legacy reads `window.matchMedia`; the catch → true).
describe('shouldAutofocusComposer', () => {
  const env = (narrow: boolean, coarse: boolean) => ({
    matchMedia: (q: string) => ({
      matches: q.includes('max-width') ? narrow : q.includes('coarse') ? coarse : false,
    }),
  })
  it('autofocuses on a wide, fine-pointer viewport', () => {
    expect(shouldAutofocusComposer(env(false, false))).toBe(true)
  })
  it('does not autofocus on a narrow viewport', () => {
    expect(shouldAutofocusComposer(env(true, false))).toBe(false)
  })
  it('does not autofocus on a coarse pointer', () => {
    expect(shouldAutofocusComposer(env(false, true))).toBe(false)
  })
  it('autofocuses when matchMedia throws', () => {
    expect(
      shouldAutofocusComposer({
        matchMedia: () => {
          throw new Error('no matchMedia')
        },
      }),
    ).toBe(true)
  })
  it('autofocuses when the env has no matchMedia', () => {
    expect(shouldAutofocusComposer({})).toBe(true)
  })
})

// Parity: chat.js:7002-7021 `_updateSendButton` (title) + 8768-8771
// `_updateStopButton` (disabled reflects the React affordance — see logic.ts).
describe('sendButtonState', () => {
  it('disables send when the input is empty (React affordance; legacy relies on _onSend no-op)', () => {
    expect(sendButtonState('', false, false).disabled).toBe(true)
    expect(sendButtonState('   ', false, false).disabled).toBe(true)
  })
  it('enables send once there is non-whitespace input', () => {
    expect(sendButtonState('hi', false, false).disabled).toBe(false)
  })
  it('labels a plain send "Send" (chat.js:7016)', () => {
    expect(sendButtonState('hi', false, false).label).toBe('Send')
  })
  it('labels a streaming send as queueing after the response (chat.js:7015)', () => {
    expect(sendButtonState('hi', true, false).label).toBe(
      'Send (queues for after current response)',
    )
  })
  it('labels a compaction-in-flight send as queueing until compaction (chat.js:7013)', () => {
    // Compaction wins over streaming (legacy ternary order, chat.js:7012-7016).
    expect(sendButtonState('hi', true, true).label).toBe('Send (queues until compaction finishes)')
    expect(sendButtonState('hi', false, true).label).toBe('Send (queues until compaction finishes)')
  })
  // Task-9 carry-forward: attachment-aware enable. Legacy `hasPayload =
  // text || _pendingAttachments.length > 0` (chat.js:6064) lets an empty
  // composer send when attachments are pending — the disable-on-empty React
  // affordance must NOT block an attachments-only send.
  it('enables send on empty text when attachments are pending (chat.js:6064)', () => {
    expect(sendButtonState('', false, false, true).disabled).toBe(false)
    expect(sendButtonState('   ', false, false, true).disabled).toBe(false)
  })
  it('still disables send on empty text with no pending attachments', () => {
    expect(sendButtonState('', false, false, false).disabled).toBe(true)
  })
})

// Parity: chat.js:304-320 — attachment mime allowlist + per-type hard caps.
describe('attachment mime + caps (parity chat.js:304-321)', () => {
  it('allows the documented mimes and rejects others (chat.js:283-287/304)', () => {
    for (const m of [
      'image/png',
      'image/jpeg',
      'image/gif',
      'image/webp',
      'application/pdf',
      'text/plain',
      'text/markdown',
      'text/html',
      'text/csv',
      'application/json',
    ]) {
      expect(isAllowedAttachmentMime(m)).toBe(true)
    }
    expect(isAllowedAttachmentMime('application/x-msdownload')).toBe(false)
    expect(isAllowedAttachmentMime('image/svg+xml')).toBe(false)
    expect(isAllowedAttachmentMime(undefined as unknown as string)).toBe(false)
  })
  it('classifies image mimes (chat.js:307)', () => {
    expect(isImageAttachmentMime('image/png')).toBe(true)
    expect(isImageAttachmentMime('application/pdf')).toBe(false)
    expect(isImageAttachmentMime('text/plain')).toBe(false)
  })
  it('classifies text-family mimes (chat.js:310)', () => {
    expect(isTextAttachmentMime('text/markdown')).toBe(true)
    expect(isTextAttachmentMime('application/json')).toBe(true)
    expect(isTextAttachmentMime('image/png')).toBe(false)
    expect(isTextAttachmentMime('application/pdf')).toBe(false)
  })
  it('only images and PDFs can stage (chat.js:313)', () => {
    expect(canStageAttachmentMime('application/pdf')).toBe(true)
    expect(canStageAttachmentMime('image/webp')).toBe(true)
    expect(canStageAttachmentMime('text/plain')).toBe(false)
    expect(canStageAttachmentMime('application/json')).toBe(false)
  })
  it('applies per-type hard caps (chat.js:316-320)', () => {
    expect(attachmentHardCapBytes('image/png')).toBe(5 * 1024 * 1024)
    expect(attachmentHardCapBytes('image/png')).toBe(ATTACHMENT_IMAGE_HARD_CAP_BYTES)
    expect(attachmentHardCapBytes('application/pdf')).toBe(30 * 1024 * 1024)
    expect(attachmentHardCapBytes('application/pdf')).toBe(ATTACHMENT_PDF_HARD_CAP_BYTES)
    expect(attachmentHardCapBytes('text/plain')).toBe(2_000_000)
    expect(attachmentHardCapBytes('text/plain')).toBe(ATTACHMENT_TEXT_HARD_CAP_BYTES)
    // Unknown mime falls through to the image cap (chat.js:320).
    expect(attachmentHardCapBytes('application/octet-stream')).toBe(ATTACHMENT_IMAGE_HARD_CAP_BYTES)
  })
  it('exposes the allowed-types label verbatim (chat.js:303)', () => {
    expect(ATTACHMENT_ALLOWED_LABEL).toBe('PNG, JPEG, GIF, WEBP, PDF, TXT, MD, HTML, CSV, JSON')
  })
})

// A minimal File-like used to exercise `resolveAttachmentMime` without relying
// on jsdom's File constructor honoring `type` (it does, but this keeps the
// helper's inputs explicit).
function fileLike(name: string, type: string, size = 10): File {
  return { name, type, size } as unknown as File
}

// Parity: chat.js:8291-8297 — resolve a file's mime, preferring an allowed
// `file.type`, else the extension map, else `file.type`, else octet-stream.
describe('resolveAttachmentMime (parity chat.js:8291)', () => {
  it('prefers an allowed file.type over the extension', () => {
    expect(resolveAttachmentMime(fileLike('photo.png', 'image/png'))).toBe('image/png')
  })
  it('falls back to the extension map when file.type is not allowed', () => {
    // A .md file that a browser reports as text/plain-ish or empty resolves via ext.
    expect(resolveAttachmentMime(fileLike('notes.md', ''))).toBe('text/markdown')
    expect(resolveAttachmentMime(fileLike('data.json', ''))).toBe('application/json')
    expect(resolveAttachmentMime(fileLike('photo.JPG', ''))).toBe('image/jpeg')
  })
  it('uses the extension even when file.type is a disallowed mime (chat.js:8295)', () => {
    // file.type present but NOT allowed → not returned; extension wins.
    expect(resolveAttachmentMime(fileLike('doc.pdf', 'application/x-pdf'))).toBe('application/pdf')
  })
  it('returns file.type then octet-stream when no extension match (chat.js:8296)', () => {
    expect(resolveAttachmentMime(fileLike('blob', 'application/weird'))).toBe('application/weird')
    expect(resolveAttachmentMime(fileLike('blob', ''))).toBe('application/octet-stream')
  })
})

// Parity: chat.js:7932-7941 — token estimate + page-dump marker score.
describe('page-dump detection helpers (parity chat.js:7932-7941)', () => {
  it('estimates tokens as floor(len/4), min 1 for non-empty (chat.js:7933)', () => {
    expect(estimateTextTokens('')).toBe(0)
    expect(estimateTextTokens('ab')).toBe(1)
    expect(estimateTextTokens('abcdefgh')).toBe(2)
  })
  it('scores page-dump markers case-insensitively (chat.js:7936-7941)', () => {
    expect(pageDumpMarkerScore('nothing here')).toBe(0)
    // Three distinct markers → score 3 (the page-dump min).
    const dump = 'CHAT SESSION with agent:main:webchat: — Still waiting for agent response'
    expect(pageDumpMarkerScore(dump)).toBe(3)
  })
})

const NOOP_TOAST = () => {}

// Parity: chat.js:7982-8050 — normalize the outgoing payload. Plain/short text
// passes through; a >=20k paste OR a >=8k page-dump (marker score >=3) becomes
// a generated .txt attachment with a canned message.
describe('normalizeOutgoingComposerPayload (parity chat.js:7982)', () => {
  it('passes short plain text through unchanged (chat.js:7995)', async () => {
    const res = await normalizeOutgoingComposerPayload('hello', [], { onToast: NOOP_TOAST })
    expect(res).not.toBeNull()
    expect(res!.text).toBe('hello')
    expect(res!.displayText).toBe('hello')
    expect(res!.attachments).toEqual([])
    expect(res!.normalized).toBeNull()
  })
  it('passes a slash command through when allowed (chat.js:7987)', async () => {
    const big = '/help ' + 'x'.repeat(LARGE_PASTE_CHARS)
    const res = await normalizeOutgoingComposerPayload(big, [], {
      allowSlashCommand: true,
      onToast: NOOP_TOAST,
    })
    expect(res!.text).toBe(big)
    expect(res!.normalized).toBeNull()
  })
  it('converts a >=20k-char large paste into a generated .txt attachment (chat.js:7986/8017)', async () => {
    const paste = 'y'.repeat(LARGE_PASTE_CHARS)
    const res = await normalizeOutgoingComposerPayload(paste, [], { onToast: NOOP_TOAST })
    expect(res).not.toBeNull()
    expect(res!.text).toBe('Please process the attached pasted text.')
    expect(res!.attachments).toHaveLength(1)
    const gen = res!.attachments[0]!
    expect(gen.generated).toBe(true)
    expect(gen.mime).toBe('text/plain')
    expect(gen.name).toMatch(/^webchat-paste-.*\.txt$/)
    expect(res!.normalized).toEqual({
      kind: 'large_paste',
      originalChars: paste.length,
      markerScore: 0,
      materialEstimatedTokens: estimateTextTokens(paste),
    })
  })
  it('converts a >=8k page dump (marker score >=3) with a page-dump message (chat.js:7985/8035)', async () => {
    const body =
      'CHAT SESSION agent:main:webchat: Still waiting for agent response\n' +
      'z'.repeat(PAGE_DUMP_CHARS)
    const res = await normalizeOutgoingComposerPayload(body, [], { onToast: NOOP_TOAST })
    expect(res!.text).toBe('Please process the attached WebChat page dump.')
    expect(res!.attachments[0]!.name).toMatch(/^webchat-page-dump-.*\.txt$/)
    expect(res!.normalized!.kind).toBe('page_dump')
  })
  it('preserves prior attachments and appends the generated one (chat.js:8042)', async () => {
    const prior: PendingAttachment = {
      kind: 'inline',
      local_id: 1,
      name: 'a.png',
      mime: 'image/png',
      size: 3,
      data: 'AAA',
      dataUrl: 'data:image/png;base64,AAA',
    }
    const paste = 'y'.repeat(LARGE_PASTE_CHARS)
    const res = await normalizeOutgoingComposerPayload(paste, [prior], { onToast: NOOP_TOAST })
    expect(res!.attachments).toHaveLength(2)
    expect(res!.attachments[0]).toMatchObject({ name: 'a.png' })
    expect(res!.attachments[1]!.generated).toBe(true)
  })
  it('rejects (returns null) + toasts when the paste exceeds the text hard cap (chat.js:8007)', async () => {
    // A paste whose UTF-8 byte length exceeds 2 MB. Use 4-byte chars to cross
    // the cap with fewer chars while still >= LARGE_PASTE_CHARS.
    const huge = '\u{1F600}'.repeat(600_000) // ~2.4 MB, > cap
    const toasts: string[] = []
    const res = await normalizeOutgoingComposerPayload(huge, [], {
      onToast: (m) => toasts.push(m),
    })
    expect(res).toBeNull()
    expect(toasts.join(' ')).toMatch(/too large to attach directly/i)
  })
})

// Parity: chat.js:8299-8325 — pending-work guard + download name/href.
describe('attachment helpers (parity chat.js:8299-8325)', () => {
  it('flags pending work while a read/upload is in flight (chat.js:8299)', () => {
    expect(
      hasPendingAttachmentWork([
        { kind: 'inline_pending', local_id: 1, name: 'a', mime: 'image/png', size: 1 },
      ]),
    ).toBe(true)
    expect(
      hasPendingAttachmentWork([
        { kind: 'uploading', local_id: 1, name: 'a', mime: 'application/pdf', size: 1 },
      ]),
    ).toBe(true)
    expect(
      hasPendingAttachmentWork([
        {
          kind: 'inline',
          local_id: 1,
          name: 'a',
          mime: 'image/png',
          size: 1,
          data: 'x',
          dataUrl: 'd',
        },
      ]),
    ).toBe(false)
    expect(hasPendingAttachmentWork([])).toBe(false)
  })
  it('resolves a download name, defaulting to "attachment" (chat.js:8308)', () => {
    expect(attachmentDownloadName({ name: 'file.txt' })).toBe('file.txt')
    expect(attachmentDownloadName({ name: '  ' })).toBe('attachment')
    expect(attachmentDownloadName({})).toBe('attachment')
  })
  it('resolves a download href, rejecting javascript: URLs (chat.js:8313-8324)', () => {
    expect(attachmentDownloadHref({ dataUrl: 'data:text/plain;base64,QQ==' }, 'text/plain')).toBe(
      'data:text/plain;base64,QQ==',
    )
    expect(attachmentDownloadHref({ data: 'QQ==' }, 'text/plain')).toBe(
      'data:text/plain;base64,QQ==',
    )
    expect(attachmentDownloadHref({ dataUrl: 'javascript:alert(1)' }, 'text/plain')).toBe('')
    expect(attachmentDownloadHref(null, 'text/plain')).toBe('')
  })
})
