import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createHistoryRenderer,
  historyMessageArtifacts,
  mergeHistoryMessagePages,
  messagePageIdentity,
  type HistoryPagingState,
  type HistoryRenderDeps,
} from './history'
import { createMessageRenderer } from './message'
import type { ChatMessage } from '../types'

function escapeHtml(value: string): string {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function pagingState(messages: ChatMessage[]): HistoryPagingState {
  return {
    loadedMessages: messages,
    oldestCursor: null,
    hasMore: false,
    scope: 'complete',
    loadingEarlier: false,
    error: '',
    compactionSummaries: [],
  }
}

function historyDeps(
  thread: HTMLElement,
  overrides: Partial<HistoryRenderDeps> = {},
): HistoryRenderDeps {
  return {
    thread: () => thread,
    esc: escapeHtml,
    displayRoleLabel: (role) => role,
    dayKey: () => '',
    dayLabel: (day) => day,
    addMessage: (role, text) => {
      const row = document.createElement('div')
      row.className = `msg ${role}`
      const body = document.createElement('div')
      body.className = 'msg-body'
      body.textContent = text
      row.appendChild(body)
      thread.appendChild(row)
      return row
    },
    replaceMessage: (row, role, text) => {
      row.className = `msg ${role}`
      const body = row.querySelector('.msg-body') || document.createElement('div')
      body.className = 'msg-body'
      body.textContent = text
      if (!body.parentNode) row.appendChild(body)
    },
    syncMessageHeader: () => {},
    attachHoverActions: (row) => {
      const body = row.querySelector('.msg-body')
      body?.querySelector(':scope > .msg-actions')?.remove()
      const actions = document.createElement('div')
      actions.className = 'msg-actions'
      body?.appendChild(actions)
    },
    reconstructToolCalls: () => {},
    renderMessageAttachmentHtml: (attachment) =>
      `<span class="msg-file-chip">${escapeHtml(String(attachment.name || 'attachment'))}</span>`,
    renderArtifacts: (artifacts) =>
      `<div class="msg-artifacts"><a class="msg-artifact-chip">${escapeHtml(
        String(artifacts[0]?.name || 'artifact'),
      )}</a></div>`,
    prepareHistoryRouterFx: () => {},
    reconcileHistoryRouterFx: () => null,
    finishHistoryRouterFx: () => {},
    markHistoryRendered: () => {},
    stampHistoryElement: () => {},
    stripProtocolTextLeak: (text) => text,
    stripDirectiveTags: (text) => text,
    stripGeneratedArtifactMarkers: (text) => text,
    stripTimePrefix: (text) => text,
    loadEarlierHistory: () => {},
    reloadHistory: () => {},
    isStreaming: () => false,
    shouldAutoScroll: () => true,
    getStreamBubble: () => null,
    getThinkingIndicator: () => null,
    getCurrentSessionLiveUserAnchor: () => null,
    getPendingFinalizedAssistantBubble: () => null,
    isPendingFinalizedAssistantBubble: () => false,
    clearPendingFinalizedAssistantBubble: () => {},
    ...overrides,
  }
}

function realHeaderSync(thread: HTMLElement) {
  return createMessageRenderer({
    thread: () => thread,
    markdown: {
      render: (text) => text,
      bindCopy: () => {},
    },
    displayRoleLabel: (role) => role.toUpperCase(),
    stampRowMeta: () => {},
    getSessionKey: () => 'agent:main:webchat:test',
    isStreaming: () => false,
    scrollToBottom: () => {},
    toast: () => {},
  }).syncMessageHeader
}

function stampHistoryTestRow(
  row: HTMLElement,
  stable: string,
  role: string,
  text: string,
  _transcriptId?: string | null,
  timestamp?: string | number | null,
): void {
  if (stable) row.dataset.messageId = stable
  else delete row.dataset.messageId
  row.dataset.historyRole = role
  row.dataset.historyRawText = text
  row.dataset.historyFallbackId = `${role}|${text}`
  row.dataset.time = timestamp ? '12:34' : ''
}

beforeEach(() => {
  document.body.innerHTML = ''
})

describe('messagePageIdentity (parity chat.js:5350)', () => {
  it('keys on the stable message_id when present', () => {
    const msg = { role: 'user', text: 'hi', message_id: 'abc' } as unknown as ChatMessage
    expect(messagePageIdentity(msg)).toBe('stable:abc')
  })

  it('falls back to id when message_id is absent', () => {
    const msg = { role: 'assistant', text: 'yo', id: 42 } as unknown as ChatMessage
    expect(messagePageIdentity(msg)).toBe('stable:42')
  })

  it('falls back to role|text identity when neither id is present', () => {
    const msg = { role: 'user', text: 'hello' } as unknown as ChatMessage
    // parity: fallback:_historyFallbackMessageIdentity(role, text) = `${role}|${text.trim()}`
    expect(messagePageIdentity(msg)).toBe('fallback:user|hello')
  })

  it('returns "" for a nullish message', () => {
    expect(messagePageIdentity(null as unknown as ChatMessage)).toBe('')
  })
})

describe('history replay scroll behavior', () => {
  it('marks every reconstructed row for one-shot final positioning', () => {
    const thread = document.createElement('div')
    document.body.appendChild(thread)
    const addMessage = vi.fn(historyDeps(thread).addMessage)
    const messages = [
      { role: 'user', text: 'first' },
      { role: 'assistant', text: 'second' },
    ] as ChatMessage[]
    const renderer = createHistoryRenderer(historyDeps(thread, { addMessage }))

    renderer.renderHistoryMessages(messages, pagingState(messages))

    expect(addMessage).toHaveBeenCalledTimes(2)
    for (const call of addMessage.mock.calls) {
      expect(call[3]).toEqual(expect.objectContaining({ autoScroll: false }))
    }
  })
})

describe('mergeHistoryMessagePages (parity chat.js:5357)', () => {
  it('prepends older messages without duplicating the overlap boundary', () => {
    const current = [
      { role: 'user', text: 'b' },
      { role: 'assistant', text: 'c' },
    ]
    const older = [
      { role: 'user', text: 'a' },
      { role: 'user', text: 'b' },
    ]
    const merged = mergeHistoryMessagePages(older as never, current as never)
    expect(merged.map((m) => m.text)).toEqual(['a', 'b', 'c']) // b deduped by identity
  })

  it('dedups the overlap boundary by stable id, keeping the older-page instance', () => {
    const older = [
      { role: 'user', text: 'A', message_id: '1' },
      { role: 'assistant', text: 'B', message_id: '2' },
    ]
    const current = [
      { role: 'assistant', text: 'B (edited)', message_id: '2' },
      { role: 'user', text: 'C', message_id: '3' },
    ]
    const merged = mergeHistoryMessagePages(older as never, current as never)
    // identity 2 appears first in older → older wins; current's dupe dropped.
    expect(merged.map((m) => (m as { message_id?: string }).message_id)).toEqual(['1', '2', '3'])
    expect(merged[1]?.text).toBe('B')
  })

  it('tolerates nullish page arguments', () => {
    const current = [{ role: 'user', text: 'x' }]
    expect(mergeHistoryMessagePages(null as never, current as never).map((m) => m.text)).toEqual([
      'x',
    ])
    expect(
      mergeHistoryMessagePages(current as never, undefined as never).map((m) => m.text),
    ).toEqual(['x'])
    expect(mergeHistoryMessagePages(null as never, undefined as never)).toEqual([])
  })

  it('dedups two id-less rows with the same role+text via the fallback identity', () => {
    // parity chat.js:5350-5354 — with no stable id, identity is
    // `fallback:${role}|${text.trim()}` (always truthy), so identical id-less
    // rows across the page boundary collapse to one.
    const older = [{ role: 'system', text: 'ping' }]
    const current = [{ role: 'system', text: 'ping' }]
    const merged = mergeHistoryMessagePages(older as never, current as never)
    expect(merged.length).toBe(1)
  })
})

describe('historyMessageArtifacts (chat.history persisted payload)', () => {
  it('returns the object artifacts carried on the message', () => {
    const artifact = {
      id: 'art-1',
      name: 'chart.png',
      mime: 'image/png',
      download_url: '/api/v1/artifacts/art-1',
    }
    const message = {
      role: 'assistant',
      text: 'done',
      artifacts: [artifact],
    } as unknown as ChatMessage

    expect(historyMessageArtifacts(message)).toEqual([artifact])
  })

  it('returns an empty list for absent, non-array, or non-object entries', () => {
    expect(historyMessageArtifacts({ role: 'assistant', text: '' })).toEqual([])
    expect(
      historyMessageArtifacts({
        role: 'assistant',
        text: '',
        artifacts: 'not-an-array',
      } as unknown as ChatMessage),
    ).toEqual([])
    expect(
      historyMessageArtifacts({
        role: 'assistant',
        text: '',
        artifacts: [null, 'bad', 3],
      } as unknown as ChatMessage),
    ).toEqual([])
  })
})

describe('createHistoryRenderer persisted rich content', () => {
  it('restores the quiet empty state when a fresh session has no history', () => {
    const thread = document.createElement('div')
    thread.innerHTML = '<div class="msg user">stale</div>'
    document.body.appendChild(thread)
    const markHistoryRendered = vi.fn()
    const renderer = createHistoryRenderer(historyDeps(thread, { markHistoryRendered }))

    renderer.renderHistoryMessages([], pagingState([]))

    expect(thread.querySelectorAll('.msg')).toHaveLength(0)
    expect(thread.querySelector('.chat-empty')).toHaveTextContent('No messages yet.')
    expect(markHistoryRendered).toHaveBeenCalledTimes(1)
  })

  it('mounts tools, attachments, artifacts, then re-attached actions in legacy order', () => {
    const thread = document.createElement('div')
    document.body.appendChild(thread)
    const calls: string[] = []
    const reconstructToolCalls = vi.fn((row: HTMLElement) => {
      calls.push('tools')
      const body = row.querySelector('.msg-body') as HTMLElement
      body.innerHTML = ''
      const text = document.createElement('div')
      text.className = 'msg-text-seg'
      text.textContent = 'answer'
      const tool = document.createElement('details')
      tool.className = 'chat-tools-collapse'
      body.append(text, tool)
    })
    const renderMessageAttachmentHtml = vi.fn((attachment: Record<string, unknown>) => {
      calls.push('attachment')
      return `<span class="msg-file-chip">${escapeHtml(String(attachment.name))}</span>`
    })
    const renderArtifacts = vi.fn((artifacts: Array<{ name?: string }>) => {
      calls.push('artifact')
      return `<div class="msg-artifacts"><a class="msg-artifact-chip">${escapeHtml(
        String(artifacts[0]?.name),
      )}</a></div>`
    })
    const attachHoverActions = vi.fn((row: HTMLElement) => {
      calls.push('actions')
      const body = row.querySelector('.msg-body') as HTMLElement
      body.querySelector(':scope > .msg-actions')?.remove()
      const actions = document.createElement('div')
      actions.className = 'msg-actions'
      body.appendChild(actions)
    })
    const messages = [
      {
        role: 'assistant',
        text: 'answer',
        tool_calls: [{ type: 'tool_use', name: 'exec_command', tool_use_id: 'tool-1' }],
        attachments: [{ name: 'input.txt', mime: 'text/plain' }],
        artifacts: [{ id: 'art-1', name: 'chart.png', mime: 'image/png' }],
      },
    ] as unknown as ChatMessage[]
    const renderer = createHistoryRenderer(
      historyDeps(thread, {
        reconstructToolCalls,
        renderMessageAttachmentHtml,
        renderArtifacts,
        attachHoverActions,
      }),
    )

    renderer.renderHistoryMessages(messages, pagingState(messages))

    const body = thread.querySelector('.msg-body') as HTMLElement
    expect(calls).toEqual(['tools', 'attachment', 'artifact', 'actions'])
    expect(Array.from(body.children).map((element) => element.className)).toEqual([
      'msg-text-seg',
      'chat-tools-collapse',
      'msg-attachments',
      'msg-artifacts',
      'msg-actions',
    ])
    expect(body).toHaveClass('msg-body--has-attachments')
    expect(body.querySelector('.msg-artifact-chip')).toHaveTextContent('chart.png')
    expect(reconstructToolCalls).toHaveBeenCalledWith(
      expect.any(HTMLElement),
      messages[0] && (messages[0] as unknown as { tool_calls: unknown[] }).tool_calls,
    )
  })

  it('escapes user text before an attachment body rewrite', () => {
    const thread = document.createElement('div')
    document.body.appendChild(thread)
    const malicious = '<img src=x onerror="alert(1)">'
    const messages = [
      {
        role: 'user',
        text: malicious,
        attachments: [{ name: 'safe.txt', mime: 'text/plain' }],
      },
    ] as unknown as ChatMessage[]
    const renderer = createHistoryRenderer(historyDeps(thread))

    renderer.renderHistoryMessages(messages, pagingState(messages))

    const body = thread.querySelector('.msg-body') as HTMLElement
    expect(body.querySelector('.msg-attachment-text')).toHaveTextContent(malicious)
    expect(body.querySelector('img')).toBeNull()
    expect(body.querySelector('.msg-attachments')).not.toBeNull()
    expect(body.lastElementChild).toHaveClass('msg-actions')
  })

  it('replays router usage oldest-to-newest with user request context', () => {
    const thread = document.createElement('div')
    document.body.appendChild(thread)
    const lifecycle: string[] = []
    const reconcileHistoryRouterFx = vi.fn(() => {
      lifecycle.push('router')
      return null
    })
    const markHistoryRendered = vi.fn(() => lifecycle.push('rendered'))
    const finishHistoryRouterFx = vi.fn(() => lifecycle.push('finished'))
    const messages = [
      { role: 'user', text: 'look', attachments: [{ mime: 'image/png', data: 'AA==' }] },
      {
        role: 'assistant',
        text: 'first',
        timestamp: 101,
        usage: {
          model: 'provider/vision',
          routed_tier: 'c1',
          routing_source: 'pilot',
          total_savings_pct: 30,
        },
      },
      { role: 'user', text: 'continue' },
      {
        role: 'assistant',
        text: 'second',
        message_id: 'assistant-2',
        usage: {
          model: 'provider/text',
          routed_tier: 'c2',
          routing_source: 'pilot',
          total_savings_pct: 40,
        },
      },
      { role: 'assistant', text: 'no usage' },
    ] as unknown as ChatMessage[]
    const renderer = createHistoryRenderer(
      historyDeps(thread, {
        turnMetaForMessage: (message) => {
          const usage = message.usage as Record<string, unknown> | undefined
          return usage
            ? { model: String(usage.model), input: 0, output: 0, saved: { ...usage } }
            : null
        },
        reconcileHistoryRouterFx,
        markHistoryRendered,
        finishHistoryRouterFx,
      }),
    )

    renderer.renderHistoryMessages(messages, pagingState(messages))

    expect(reconcileHistoryRouterFx).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ routed_tier: 'c1' }),
      { turnIndex: 1, requestKind: 'image', hintTimestamp: 101 },
    )
    expect(reconcileHistoryRouterFx).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ routed_tier: 'c2' }),
      { turnIndex: 2, requestKind: 'text', hintTimestamp: 'assistant-2' },
    )
    expect(lifecycle.slice(-2)).toEqual(['rendered', 'finished'])
  })

  it('backfills the outer turn-meta model before replaying router usage', () => {
    const thread = document.createElement('div')
    document.body.appendChild(thread)
    const reconcileHistoryRouterFx = vi.fn(() => null)
    const messages = [
      { role: 'user', text: 'hello' },
      { role: 'assistant', text: 'legacy cached footer' },
    ] as ChatMessage[]
    const renderer = createHistoryRenderer(
      historyDeps(thread, {
        turnMetaForMessage: () => ({
          model: 'provider/legacy-model',
          input: 4,
          output: 2,
          saved: {
            routed_tier: 'c1',
            routing_source: 'pilot',
            total_savings_pct: 25,
          },
        }),
        reconcileHistoryRouterFx,
      }),
    )

    renderer.renderHistoryMessages(messages, pagingState(messages))

    expect(reconcileHistoryRouterFx).toHaveBeenCalledWith(
      expect.objectContaining({ model: 'provider/legacy-model' }),
      expect.objectContaining({ turnIndex: 1 }),
    )
  })

  it('reuses matching rows and prunes stale rows without duplicating a live tail', () => {
    const thread = document.createElement('div')
    document.body.appendChild(thread)
    const existing = document.createElement('div')
    existing.className = 'msg user'
    existing.dataset.messageId = 'user-1'
    existing.dataset.historyRole = 'user'
    existing.dataset.historyRawText = 'old draft'
    existing.innerHTML = '<div class="msg-body">old draft</div>'
    const stale = document.createElement('div')
    stale.className = 'msg assistant'
    stale.dataset.messageId = 'stale'
    stale.innerHTML = '<div class="msg-body">stale</div>'
    const liveBubble = document.createElement('div')
    liveBubble.className = 'msg assistant streaming'
    liveBubble.innerHTML = '<div class="msg-body">live</div>'
    thread.append(existing, stale, liveBubble)
    const messages = [
      { role: 'user', text: 'persisted', message_id: 'user-1' },
    ] as unknown as ChatMessage[]
    const renderer = createHistoryRenderer(
      historyDeps(thread, {
        isStreaming: () => true,
        getStreamBubble: () => liveBubble,
        getCurrentSessionLiveUserAnchor: () => existing,
        stampHistoryElement: (row, stable, role, text) => {
          row.dataset.messageId = stable
          row.dataset.historyRole = role
          row.dataset.historyRawText = text
        },
      }),
    )

    renderer.renderHistoryMessages(messages, pagingState(messages))

    expect(thread.querySelector('[data-message-id="user-1"]')).toBe(existing)
    expect(existing.querySelector('.msg-body')).toHaveTextContent('persisted')
    expect(stale.isConnected).toBe(false)
    expect(liveBubble.isConnected).toBe(true)
    expect(thread.querySelectorAll('.msg')).toHaveLength(2)
    expect(thread.lastElementChild).toBe(liveBubble)
  })

  it('keeps a pending finalized assistant until history catches up, then consumes it once', () => {
    const thread = document.createElement('div')
    document.body.appendChild(thread)
    const pending = document.createElement('div')
    pending.className = 'msg assistant'
    pending.dataset.historyRole = 'assistant'
    pending.dataset.historyRawText = 'final answer'
    pending.dataset.historyFallbackId = 'assistant|final answer'
    pending.dataset.pendingFinalizedAssistant = 'true'
    pending.innerHTML = '<div class="msg-body">final answer</div>'
    thread.appendChild(pending)
    const clearPending = vi.fn(() => {
      delete pending.dataset.pendingFinalizedAssistant
    })
    const deps = historyDeps(thread, {
      getPendingFinalizedAssistantBubble: () => pending,
      isPendingFinalizedAssistantBubble: (row) =>
        row === pending && pending.dataset.pendingFinalizedAssistant === 'true',
      clearPendingFinalizedAssistantBubble: clearPending,
      stampHistoryElement: (row, stable, role, text) => {
        if (stable) row.dataset.messageId = stable
        row.dataset.historyRole = role
        row.dataset.historyRawText = text
        row.dataset.historyFallbackId = `${role}|${text}`
      },
    })
    const renderer = createHistoryRenderer(deps)
    const waiting = [
      { role: 'user', text: 'question', message_id: 'user-1' },
    ] as unknown as ChatMessage[]

    renderer.renderHistoryMessages(waiting, pagingState(waiting))
    expect(pending.isConnected).toBe(true)
    expect(clearPending).not.toHaveBeenCalled()

    const caughtUp = waiting.concat({
      role: 'assistant',
      text: 'final answer',
      message_id: 'assistant-1',
    } as unknown as ChatMessage)
    renderer.renderHistoryMessages(caughtUp, pagingState(caughtUp))

    expect(thread.querySelector('[data-message-id="assistant-1"]')).toBe(pending)
    expect(thread.querySelectorAll('.msg.assistant')).toHaveLength(1)
    expect(clearPending).toHaveBeenCalledTimes(1)
  })

  it('reconciles reused headers when prepending the same user and day', () => {
    const thread = document.createElement('div')
    document.body.appendChild(thread)
    const day = '2026-07-21'
    const initial = [
      { role: 'user', text: 'u2', message_id: 'u2', timestamp: `${day}T10:00:00Z` },
      { role: 'user', text: 'u3', message_id: 'u3', timestamp: `${day}T10:01:00Z` },
    ] as unknown as ChatMessage[]
    const renderer = createHistoryRenderer(
      historyDeps(thread, {
        dayKey: (timestamp) => String(timestamp).slice(0, 10),
        dayLabel: (value) => value,
        stampHistoryElement: stampHistoryTestRow,
        syncMessageHeader: realHeaderSync(thread),
      }),
    )

    renderer.renderHistoryMessages(initial, pagingState(initial))
    const u2 = thread.querySelector<HTMLElement>('[data-message-id="u2"]')
    const u3 = thread.querySelector<HTMLElement>('[data-message-id="u3"]')
    expect(thread.querySelectorAll('.msg-header')).toHaveLength(1)

    const prepended = [
      { role: 'user', text: 'u1', message_id: 'u1', timestamp: `${day}T09:59:00Z` },
      ...initial,
    ] as unknown as ChatMessage[]
    renderer.renderHistoryMessages(prepended, pagingState(prepended), { preserveScroll: true })

    expect(thread.querySelector('[data-message-id="u2"]')).toBe(u2)
    expect(thread.querySelector('[data-message-id="u3"]')).toBe(u3)
    expect(thread.querySelectorAll('.msg-header')).toHaveLength(1)
    expect(thread.querySelector('[data-message-id="u1"] .role-label')).toHaveTextContent('USER')
    expect(u2?.querySelector(':scope > .msg-header')).toBeNull()
    expect(u3?.querySelector(':scope > .msg-header')).toBeNull()
  })

  it('creates and removes a reused header as its day boundary changes', () => {
    const thread = document.createElement('div')
    document.body.appendChild(thread)
    const renderer = createHistoryRenderer(
      historyDeps(thread, {
        dayKey: (timestamp) => String(timestamp).slice(0, 10),
        dayLabel: (value) => value,
        stampHistoryElement: stampHistoryTestRow,
        syncMessageHeader: realHeaderSync(thread),
      }),
    )
    const dayOne = '2026-07-20'
    const initial = [
      { role: 'assistant', text: 'a1', message_id: 'a1', timestamp: `${dayOne}T10:00:00Z` },
      { role: 'assistant', text: 'a2', message_id: 'a2', timestamp: `${dayOne}T10:01:00Z` },
    ] as unknown as ChatMessage[]

    renderer.renderHistoryMessages(initial, pagingState(initial))
    const a2 = thread.querySelector<HTMLElement>('[data-message-id="a2"]')!
    expect(a2.querySelector(':scope > .msg-header')).toBeNull()
    expect(a2.title).not.toBe('')

    const splitAcrossDays = [
      initial[0],
      {
        role: 'assistant',
        text: 'a2',
        message_id: 'a2',
        timestamp: '2026-07-21T10:01:00Z',
        provenance_kind: 'cron',
      },
    ] as unknown as ChatMessage[]
    renderer.renderHistoryMessages(splitAcrossDays, pagingState(splitAcrossDays))

    expect(thread.querySelector('[data-message-id="a2"]')).toBe(a2)
    expect(a2.querySelector(':scope > .msg-header .cron-tag')).toHaveTextContent('Cron')
    expect(a2.querySelector<HTMLElement>(':scope > .msg-header')?.title).not.toBe('')
    expect(a2.hasAttribute('title')).toBe(false)

    renderer.renderHistoryMessages(initial, pagingState(initial))

    expect(thread.querySelector('[data-message-id="a2"]')).toBe(a2)
    expect(a2.querySelector(':scope > .msg-header')).toBeNull()
    expect(a2.querySelector('.cron-tag')).toBeNull()
    expect(a2.title).not.toBe('')
  })
})
