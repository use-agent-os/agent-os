import { readFileSync } from 'node:fs'
import { describe, it, expect, vi } from 'vitest'
import {
  createToolRenderer,
  toolDisplayName,
  fmtToolDuration,
  toolResultIsError,
  toolResultIsTruncated,
  parseSubagentCompletion,
  isControlPlaneToolName,
} from './tools'

const chatCss = readFileSync('src/views/chat/chat.css', 'utf8')

function createToolHarness() {
  const bubble = document.createElement('div')
  const body = document.createElement('div')
  body.className = 'msg-body'
  bubble.appendChild(body)

  const renderer = createToolRenderer({
    ensureStreamBubble: () => bubble,
    markVisibleStreamEvent: () => {},
    flushPendingTextSegment: () => {},
    newTextSegment: () => document.createElement('div'),
    scrollToBottom: () => {},
    getAutoScroll: () => false,
    pushSegment: () => {},
    getSearchProvider: () => '',
    setSearchProvider: () => {},
    getSessionKey: () => 'agent:main:webchat:test',
    addMessage: () => null,
    pushMessage: () => {},
  })

  return { body, renderer }
}

// Pure-helper parity tests. Every asserted string/format is confirmed against
// the legacy source (static/js/views/chat.js) at the cited line, NOT guessed.

describe('toolDisplayName (parity chat.js:7049)', () => {
  it('returns the raw name for a non-publish tool', () => {
    expect(toolDisplayName('bash', '')).toBe('bash')
  })

  it('falls back to "tool" for an empty name', () => {
    expect(toolDisplayName('', '')).toBe('tool')
  })

  it('appends the basename target for publish_artifact (object input)', () => {
    // chat.js:7050-7053 → `${name} - ${basename(name||path)}`
    expect(toolDisplayName('publish_artifact', { path: '/tmp/out/report.md' })).toBe(
      'publish_artifact - report.md',
    )
  })

  it('parses a JSON-string input for publish_artifact and prefers name over path', () => {
    expect(toolDisplayName('publish_artifact', '{"name":"final.html","path":"/x/y.html"}')).toBe(
      'publish_artifact - final.html',
    )
  })

  it('returns the bare name for publish_artifact when no target resolves', () => {
    expect(toolDisplayName('publish_artifact', '')).toBe('publish_artifact')
  })
})

describe('fmtToolDuration (parity chat.js:7107)', () => {
  it('returns "" for falsy or negative input', () => {
    expect(fmtToolDuration(0)).toBe('')
    expect(fmtToolDuration(-5)).toBe('')
  })

  it('renders sub-10s durations to one decimal second', () => {
    // chat.js:7110 — s < 10 → `${s.toFixed(1)}s`
    expect(fmtToolDuration(450)).toBe('0.5s')
    expect(fmtToolDuration(1500)).toBe('1.5s')
  })

  it('rounds 10s..60s to whole seconds', () => {
    // chat.js:7111 — s < 60 → `${Math.round(s)}s`
    expect(fmtToolDuration(12000)).toBe('12s')
    expect(fmtToolDuration(59400)).toBe('59s')
  })

  it('renders >=60s as `${m}m${s}s`', () => {
    // chat.js:7112 — `${Math.floor(s/60)}m${Math.round(s%60)}s`
    expect(fmtToolDuration(75000)).toBe('1m15s')
    expect(fmtToolDuration(125000)).toBe('2m5s')
  })
})

describe('toolResultIsError (parity chat.js:7206)', () => {
  it('is true when execution_status.status is error/timeout/cancelled', () => {
    expect(toolResultIsError({ execution_status: { status: 'error' } })).toBe(true)
    expect(toolResultIsError({ execution_status: { status: 'timeout' } })).toBe(true)
    expect(toolResultIsError({ execution_status: { status: 'cancelled' } })).toBe(true)
  })

  it('is false when execution_status.status is a success-ish string', () => {
    // A present status string short-circuits the is_error fallback.
    expect(toolResultIsError({ execution_status: { status: 'success' } })).toBe(false)
    expect(toolResultIsError({ execution_status: { status: 'success' }, is_error: true })).toBe(
      false,
    )
  })

  it('falls back to is_error/isError/error flags when no status object', () => {
    expect(toolResultIsError({ is_error: true })).toBe(true)
    expect(toolResultIsError({ isError: true })).toBe(true)
    expect(toolResultIsError({ error: 'boom' })).toBe(true)
    expect(toolResultIsError({})).toBe(false)
  })
})

describe('toolResultIsTruncated (parity chat.js:7221)', () => {
  it('is true only when execution_status.truncated is truthy', () => {
    expect(toolResultIsTruncated({ execution_status: { truncated: true } })).toBe(true)
    expect(toolResultIsTruncated({ execution_status: { truncated: false } })).toBe(false)
    expect(toolResultIsTruncated({ truncated: true })).toBe(false)
    expect(toolResultIsTruncated({})).toBe(false)
  })
})

describe('parseSubagentCompletion (parity chat.js:7817)', () => {
  it('parses a subagent_completion JSON payload', () => {
    const text = JSON.stringify({ type: 'subagent_completion', child_session_key: 'a:b' })
    expect(parseSubagentCompletion(text)).toEqual({
      type: 'subagent_completion',
      child_session_key: 'a:b',
    })
  })

  it('returns null for JSON of a different type', () => {
    expect(parseSubagentCompletion('{"type":"other"}')).toBeNull()
  })

  it('returns null for non-JSON text', () => {
    expect(parseSubagentCompletion('not json')).toBeNull()
  })
})

describe('isControlPlaneToolName (parity chat.js:7057)', () => {
  it('is true only for router_control', () => {
    expect(isControlPlaneToolName('router_control')).toBe(true)
    expect(isControlPlaneToolName('bash')).toBe(false)
    expect(isControlPlaneToolName('')).toBe(false)
  })
})

describe('tool activity DOM states', () => {
  it('renders a compact running row with an accessible status and blocks expansion', () => {
    const { body, renderer } = createToolHarness()
    renderer.appendToolCall({
      name: 'skill_view',
      tool_use_id: 'tool-running',
      input: { skill: 'bankr' },
    })

    const details = body.querySelector<HTMLDetailsElement>('.chat-tools-collapse')!
    const summary = details.querySelector<HTMLElement>('.chat-tools-summary')!
    const status = summary.querySelector<HTMLElement>('.chat-tools-status')!

    expect(details).toHaveClass('chat-tools-collapse--running')
    expect(details).toHaveAttribute('data-tool-name', 'skill_view')
    expect(summary).toHaveAttribute('aria-disabled', 'true')
    expect(status).toHaveAttribute('data-status', 'running')
    expect(status).toHaveAttribute('aria-label', 'Running')
    expect(details.querySelector('.chat-tool-input')).toHaveTextContent('"skill": "bankr"')

    const click = new MouseEvent('click', { bubbles: true, cancelable: true })
    expect(summary.dispatchEvent(click)).toBe(false)
    expect(details.open).toBe(false)
  })

  it('settles a successful row with elapsed time, result content, and expansion enabled', () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(1_000)
      const { body, renderer } = createToolHarness()
      renderer.appendToolCall({
        name: 'exec_command',
        tool_use_id: 'tool-success',
        input: { cmd: 'bankr status' },
      })

      vi.setSystemTime(2_500)
      renderer.appendToolResult({
        name: 'exec_command',
        tool_use_id: 'tool-success',
        result: 'authenticated',
        execution_status: { status: 'success' },
      })

      const details = body.querySelector<HTMLDetailsElement>('[data-tool-id="tool-success"]')!
      const summary = details.querySelector<HTMLElement>('.chat-tools-summary')!
      const status = summary.querySelector<HTMLElement>('.chat-tools-status')!

      expect(details).not.toHaveClass('chat-tools-collapse--running')
      expect(details).toHaveClass('chat-tools-collapse--success')
      expect(summary).not.toHaveAttribute('aria-disabled')
      expect(status).toHaveAttribute('data-status', 'success')
      expect(status).toHaveAttribute('aria-label', 'Completed in 1.5s')
      expect(status).toHaveTextContent('1.5s')
      expect(details.querySelector('.chat-tool-result-preview')).toHaveTextContent('authenticated')
    } finally {
      vi.useRealTimers()
    }
  })

  it('settles a failed row with an accessible failure state', () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(5_000)
      const { body, renderer } = createToolHarness()
      renderer.appendToolCall({
        name: 'exec_command',
        tool_use_id: 'tool-error',
        input: { cmd: 'exit 1' },
      })

      vi.setSystemTime(5_500)
      renderer.appendToolResult({
        name: 'exec_command',
        tool_use_id: 'tool-error',
        result: 'permission denied',
        execution_status: { status: 'error' },
      })

      const details = body.querySelector<HTMLDetailsElement>('[data-tool-id="tool-error"]')!
      const status = details.querySelector<HTMLElement>('.chat-tools-status')!

      expect(details).toHaveClass('chat-tools-collapse--error')
      expect(status).toHaveAttribute('data-status', 'error')
      expect(status).toHaveAttribute('aria-label', 'Failed in 0.5s')
      expect(details.querySelector('.chat-tool-result')).toHaveClass('chat-tool-result--error')
      expect(details.querySelector('.chat-tool-result-preview')).toHaveTextContent(
        'permission denied',
      )
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('tool activity stylesheet contract', () => {
  it('replaces native disclosure chrome with the compact legacy row treatment', () => {
    expect(chatCss).toContain('.chat-tools-collapse {')
    expect(chatCss).toContain('border-left: 1px solid var(--border)')
    expect(chatCss).toContain('.chat-tools-summary::-webkit-details-marker')
    expect(chatCss).toContain('.chat-tools-collapse[open] > .chat-tools-summary::after')
    expect(chatCss).toContain('.chat-tools-status {')
  })

  it('styles every tool state and provides a reduced-motion fallback', () => {
    expect(chatCss).toContain('.chat-tools-collapse--running')
    expect(chatCss).toContain('.chat-tools-collapse--success')
    expect(chatCss).toContain('.chat-tools-collapse--error')
    expect(chatCss).toContain('.chat-tools-collapse--unknown')
    expect(chatCss).toContain('@keyframes chat-tool-spin')
    expect(chatCss).toContain('@media (prefers-reduced-motion: reduce)')
  })
})
