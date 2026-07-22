import { readFileSync } from 'node:fs'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMessageRenderer, historyTurnMeta } from './message'

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
    expect(row.querySelector('.msg-body')).not.toHaveTextContent('invoke')
    expect(row.querySelector('.cron-tag')).toHaveTextContent('Cron')
    expect(row.querySelectorAll('.msg-action')).toHaveLength(2)
    expect(row.querySelector('[aria-label="Regenerate response"]')).not.toBeNull()
  })

  it('renders subagent JSON through textContent inside a disclosure', () => {
    const { renderer } = makeRenderer()
    const payload = JSON.stringify({
      type: 'subagent_completion',
      child_session_key: 'agent:child',
      value: '<img src=x onerror=alert(1)>',
    })
    const row = renderer.addMessage('system', payload, Date.now(), {
      provenanceSourceTool: 'subagent_completion',
    })!

    expect(row).toHaveClass('subagent')
    expect(row.querySelector('summary')).toHaveTextContent('Subagent: agent:child')
    expect(row.querySelector('img')).toBeNull()
    expect(row.querySelector('pre')).toHaveTextContent('<img src=x onerror=alert(1)>')
  })

  it('attaches model, token, cost, cache, reasoning, and savings metadata', () => {
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
      'model↑1.3k ↓42cache:500think:12$0.00125Saved ~51%',
    )
    expect(row.querySelector('.msg-meta__saved')).toHaveClass('msg-meta__saved--high')
    expect(row.querySelector('.msg-meta__saved-flame path')).toHaveAttribute('fill', 'currentColor')
  })

  it('renders saved intensity, live flash, and the current combo snapshot', () => {
    const { renderer } = makeRenderer({
      getSavingsStreak: () => ({ current: 5, max: 5 }),
      savingsLabel: (pct: number) => `Saved ~${Math.round(pct)}%`,
    })
    const row = renderer.addMessage('assistant', 'done')!
    renderer.attachTurnMeta(
      row,
      'provider/model',
      10,
      2,
      {
        routed_tier: 'c1',
        routing_source: 'pilot',
        total_savings_pct: 70,
      },
      { flash: true },
    )

    const saved = row.querySelector('.msg-meta__saved')!
    expect(saved).toHaveClass('msg-meta__saved--peak', 'msg-meta__saved--flash')
    saved.dispatchEvent(new Event('animationend'))
    expect(saved).not.toHaveClass('msg-meta__saved--flash')
    expect(row.querySelector('.msg-meta__combo')).toHaveClass('msg-meta__combo--blaze')
    expect(row.querySelector('.msg-meta__combo')).toHaveAttribute('aria-label', 'Combo 5')
    expect(row.querySelector('.msg-meta__combo')).toHaveTextContent('COMBO×5')
  })

  it('uses hot combo at x3, base combo at x2, and never fabricates unsupported savings', () => {
    const makeUsage = () => ({
      routed_tier: 'c1',
      routing_source: 'pilot',
      total_savings_pct: 30,
    })
    const base = makeRenderer({ getSavingsStreak: () => ({ current: 2, max: 2 }) })
    const baseRow = base.renderer.addMessage('assistant', 'base')!
    base.renderer.attachTurnMeta(baseRow, '', 0, 0, makeUsage())
    expect(baseRow.querySelector('.msg-meta__combo')).not.toHaveClass(
      'msg-meta__combo--hot',
      'msg-meta__combo--blaze',
    )

    const hot = makeRenderer({ getSavingsStreak: () => ({ current: 3, max: 3 }) })
    const hotRow = hot.renderer.addMessage('assistant', 'hot')!
    hot.renderer.attachTurnMeta(hotRow, '', 0, 0, makeUsage())
    expect(hotRow.querySelector('.msg-meta__combo')).toHaveClass('msg-meta__combo--hot')

    const unsupported = makeRenderer({ getSavingsStreak: () => ({ current: 9, max: 9 }) })
    const unsupportedRow = unsupported.renderer.addMessage('assistant', 'plain')!
    unsupported.renderer.attachTurnMeta(unsupportedRow, 'provider/model', 1, 1, {
      routed_tier: 'c1',
      routing_source: 'pilot',
    })
    expect(unsupportedRow.querySelector('.msg-meta__saved')).toBeNull()
    expect(unsupportedRow.querySelector('.msg-meta__combo')).toBeNull()
  })

  it('hides saved and combo metadata on an identity-switch suppression turn', () => {
    const { renderer } = makeRenderer({
      getSavingsStreak: () => ({ current: 1, max: 2 }),
    })
    const row = renderer.addMessage('assistant', 'switched')!
    renderer.attachTurnMeta(row, 'provider/model', 1, 1, {
      routed_tier: 'c1',
      routing_source: 'pilot',
      total_savings_pct: 55,
      __savings_ui_suppressed: true,
    })

    expect(row.querySelector('.msg-meta__saved')).toBeNull()
    expect(row.querySelector('.msg-meta__combo')).toBeNull()
  })

  it('does not coerce a malformed persisted savings percentage', () => {
    const { renderer } = makeRenderer({
      getSavingsStreak: () => ({ current: 7, max: 7 }),
    })
    const row = renderer.addMessage('assistant', 'malformed')!
    renderer.attachTurnMeta(row, 'provider/model', 1, 1, {
      routed_tier: 'c1',
      routing_source: 'pilot',
      total_savings_pct: '51' as unknown as number,
    })

    expect(row.querySelector('.msg-meta__saved')).toBeNull()
    expect(row.querySelector('.msg-meta__combo')).toBeNull()
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

describe('SavingsFX footer CSS contract', () => {
  it('keeps saved tiers, flash, combo tiers, popup fallback, and reduced-motion rules styled', () => {
    expect(chatCss).toContain('.msg-meta__saved--high')
    expect(chatCss).toContain('.msg-meta__saved--peak')
    expect(chatCss).toContain('.msg-meta__saved--flash .msg-meta__saved-label')
    expect(chatCss).toContain('.msg-meta__combo--hot')
    expect(chatCss).toContain('.msg-meta__combo--blaze')
    expect(chatCss).toContain('.savings-float')
    expect(chatCss).toContain('@media (prefers-reduced-motion: reduce)')
    expect(chatCss).toMatch(/\.msg-meta__saved\s*\{[\s\S]*border-radius: var\(--radius\)/)
  })
})
