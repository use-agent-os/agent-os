import { readFileSync } from 'node:fs'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render } from '../markdown'
import { createMessageRenderer, historyTurnMeta, systemRowTone } from './message'
import { createThinkingBlock } from './thinking'

const chatCss = readFileSync('src/views/chat/chat.css', 'utf8')

function makeRenderer(overrides: Record<string, unknown> = {}) {
  const thread = document.createElement('div')
  const renderer = createMessageRenderer({
    thread: () => thread,
    markdown: {
      render: (text) => `<p>${text.replace(/</g, '&lt;')}</p>`,
      bindCopy: vi.fn(),
      bindHighlight: vi.fn(),
    },
    displayRoleLabel: (role) => role.toUpperCase(),
    stampRowMeta: (row, role) => {
      row.dataset.sender = role.toUpperCase()
      row.dataset.time = '12:34'
    },
    getSessionKey: () => 'agent:main:webchat:default',
    isStreaming: () => false,
    scrollToBottom: vi.fn(),
    toast: vi.fn(),
    ...overrides,
  })
  return { thread, renderer }
}

beforeEach(() => {
  document.body.innerHTML = ''
})

describe('message renderer', () => {
  it('can suppress per-row auto-scroll during a bulk history replay', () => {
    const scrollToBottom = vi.fn()
    const { renderer } = makeRenderer({ scrollToBottom })

    renderer.addMessage('assistant', 'persisted row', null, { autoScroll: false })
    expect(scrollToBottom).not.toHaveBeenCalled()

    renderer.addMessage('assistant', 'live row')
    expect(scrollToBottom).toHaveBeenCalledTimes(1)
  })

  it('keeps live rows on the correct side of a shared day boundary', () => {
    const headerState = { current: { day: '', role: '' } }
    const { thread, renderer } = makeRenderer({
      headerState,
      dayKey: (timestamp: string | number | null | undefined) =>
        String(timestamp || '').slice(0, 10),
      dayLabel: (day: string) => day,
    })

    const first = renderer.addMessage('user', 'before midnight', '2026-07-21T23:59:59Z')!
    const second = renderer.addMessage('user', 'after midnight', '2026-07-22T00:00:01Z')!

    const children = [...thread.children]
    expect(thread.querySelectorAll('.chat-day-sep')).toHaveLength(2)
    expect(children.indexOf(thread.querySelectorAll('.chat-day-sep')[1]!)).toBeLessThan(
      children.indexOf(second),
    )
    expect(first.querySelector('.msg-header')).not.toBeNull()
    expect(second.querySelector('.msg-header')).not.toBeNull()
    expect(headerState.current).toEqual({ day: '2026-07-22', role: 'user' })
  })

  it('renders a sanitized assistant body, cron tag, and real hover actions', () => {
    const onRegenerate = vi.fn()
    const { renderer } = makeRenderer({ onRegenerate })
    const row = renderer.addMessage(
      'assistant',
      'answer [[reply_to_current]] <invoke name="tool"><parameter name="command">x</parameter></invoke>',
      '2026-07-22T12:34:00Z',
      { provenanceKind: 'cron' },
    )!

    expect(row.querySelector('.msg-body')).toHaveTextContent('answer')
    expect(row.querySelector('.msg-body')).not.toHaveTextContent('reply_to_current')
    // A tool call left in the text never ran, so it is shown as written.
    expect(row.querySelector('.msg-body')).toHaveTextContent('<invoke name="tool">')
    expect(row.querySelector('.cron-tag')).toHaveTextContent('Cron')
    expect(row.querySelectorAll('.msg-action')).toHaveLength(2)
    expect(row.querySelector('[aria-label="Regenerate response"]')).not.toBeNull()
  })

  it('renders a subagent completion as a card, its payload only ever as text', () => {
    const { renderer } = makeRenderer()
    const payload = JSON.stringify({
      type: 'subagent_completion',
      child_session_key: 'agent:child:webchat:x',
      status: 'succeeded',
      value: '<img src=x onerror=alert(1)>',
    })
    const row = renderer.addMessage('system', payload, Date.now(), {
      provenanceSourceTool: 'subagent_completion',
    })!

    expect(row).toHaveClass('subagent')
    expect(row.querySelector('.acp-done__title')).toHaveTextContent('Subagent child finished')
    expect(row.querySelector('img')).toBeNull()
    // The raw payload stays reachable for debugging, behind "Details", as text.
    expect(row.querySelector('.acp-details__json')).toHaveTextContent(
      '<img src=x onerror=alert(1)>',
    )
    expect(row.querySelector('details')).toBeNull()
  })

  it('keeps a raw disclosure for a completion row that is not a JSON object', () => {
    const { renderer } = makeRenderer()
    const row = renderer.addMessage('system', 'not json <b>x</b>', Date.now(), {
      provenanceSourceTool: 'subagent_completion',
    })!
    expect(row.querySelector('summary')).toHaveTextContent('Subagent completion')
    expect(row.querySelector('pre')).toHaveTextContent('not json <b>x</b>')
    expect(row.querySelector('b')).toBeNull()
  })

  it('hands the completion payload to the injected card builder', () => {
    const subagentCard = vi.fn(() => {
      const el = document.createElement('div')
      el.className = 'injected-card'
      return el
    })
    const { renderer } = makeRenderer({ subagentCard })
    const text = JSON.stringify({ type: 'subagent_completion', child_session_key: 'k' })
    const row = renderer.addMessage('system', text, Date.now(), {
      provenanceSourceTool: 'subagent_completion',
    })!
    expect(subagentCard).toHaveBeenCalledWith(
      { type: 'subagent_completion', child_session_key: 'k' },
      text,
    )
    expect(row.querySelector('.injected-card')).not.toBeNull()
  })

  it('marks a persisted "Error: …" system row so the skins draw it as an error', () => {
    const { renderer } = makeRenderer()
    const failed = renderer.addMessage('system', 'Error: ACP session was closed')!
    expect(failed.querySelector('.msg-body')).toHaveAttribute('data-tone', 'error')
    const note = renderer.addMessage('system', 'Context compacted')!
    expect(note.querySelector('.msg-body')).not.toHaveAttribute('data-tone')
  })

  it('draws a stopped turn ("Cancelled after …") as a notice, never as an error', () => {
    const { renderer } = makeRenderer()
    const row = renderer.addMessage('system', 'Cancelled after 6 tool calls (1:42).')!
    expect(row.querySelector('.msg-body')).toHaveAttribute('data-tone', 'notice')
    expect(systemRowTone('Error: boom')).toBe('error')
    expect(systemRowTone('Canceled by the user')).toBe('notice')
    expect(systemRowTone('Cancellation policy changed')).toBe('')
  })

  it('attaches model, token, cost, cache, and reasoning metadata', () => {
    const { renderer } = makeRenderer()
    const row = renderer.addMessage('assistant', 'done')!
    renderer.attachTurnMeta(row, 'openrouter/vendor/model-20260722', 1_250, 42, {
      cached_tokens: 500,
      reasoning_tokens: 12,
      cost_usd: 0.00125,
      routed_tier: 'fast',
      routing_source: 'pilot',
      total_savings_pct: 51,
    })

    expect(row.querySelector('.msg-meta')).toHaveTextContent(
      'model↑1.3k ↓42cache:500think:12$0.00125',
    )
  })

  it('copies only the reply text when the bubble carries a reasoning block', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    const { renderer } = makeRenderer()
    const row = renderer.addMessage('assistant', 'the reply', '2026-07-22T12:34:00Z')!
    const body = row.querySelector<HTMLElement>('.msg-body')!
    const parts = createThinkingBlock({ open: false })
    body.insertAdjacentElement('afterbegin', parts.details)

    // Collapsed: the `Thinking` summary label must not leak into the clipboard.
    expect(renderer.extractBubbleText(row)).toBe('the reply')

    // Expanded: neither does the reasoning body fetched on first expand.
    parts.details.open = true
    parts.content.textContent = 'chain of thought that must stay private'
    expect(renderer.extractBubbleText(row)).toBe('the reply')

    row.querySelector<HTMLButtonElement>('.msg-action[data-action="copy"]')!.click()
    await Promise.resolve()
    expect(writeText).toHaveBeenCalledWith('the reply')
  })

  it('copies a markdown reply with the line breaks it is drawn with', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    const { renderer } = makeRenderer({
      markdown: { render, bindCopy: vi.fn(), bindHighlight: vi.fn() },
    })
    const reply =
      'AgentOS 2026.9.28 🧢\n🦄 Uniswap V4 LP\n🍎 Native builds\n\n```bash\nuv sync\n```'
    const row = renderer.addMessage('assistant', reply, '2026-07-22T12:34:00Z')!

    row.querySelector<HTMLButtonElement>('.msg-action[data-action="copy"]')!.click()
    await Promise.resolve()
    // The fenced block's language label and its own Copy button stay out.
    expect(writeText).toHaveBeenCalledWith(
      'AgentOS 2026.9.28 🧢\n🦄 Uniswap V4 LP\n🍎 Native builds\n\nuv sync',
    )
  })

  it('copies a user bubble verbatim', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    const { renderer } = makeRenderer()
    const row = renderer.addMessage('user', 'first line\n\n  second   line')!

    row.querySelector<HTMLButtonElement>('.msg-action[data-action="copy"]')!.click()
    await Promise.resolve()
    expect(writeText).toHaveBeenCalledWith('first line\n\n  second   line')
  })

  it('does not render the retired savings or combo UI from usage payloads', () => {
    const { renderer } = makeRenderer()
    const row = renderer.addMessage('assistant', 'done')!
    renderer.attachTurnMeta(row, 'provider/model', 10, 2, {
      routed_tier: 'c1',
      routing_source: 'pilot',
      total_savings_pct: 70,
    })
    expect(row.querySelector('.msg-meta__saved')).toBeNull()
    expect(row.querySelector('.msg-meta__combo')).toBeNull()
    expect(row.querySelector('.msg-meta')).toHaveTextContent('model↑10 ↓2')
  })
})

describe('historyTurnMeta', () => {
  it('reads the real history usage aliases and returns null when absent', () => {
    expect(
      historyTurnMeta({
        usage: { model: 'provider/model', inputTokens: 10, output_tokens: 4, cost_usd: 0.2 },
      }),
    ).toMatchObject({ model: 'provider/model', input: 10, output: 4 })
    expect(historyTurnMeta({ text: 'no usage' })).toBeNull()
  })
})

describe('retired SavingsFX CSS contract', () => {
  it('does not ship saved, combo, or popup presentation rules', () => {
    expect(chatCss).not.toContain('.msg-meta__saved')
    expect(chatCss).not.toContain('.msg-meta__combo')
    expect(chatCss).not.toContain('.savings-float')
    expect(chatCss).not.toContain('@keyframes chat-savings')
  })
})
