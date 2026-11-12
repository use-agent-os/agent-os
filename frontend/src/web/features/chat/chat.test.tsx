import '@/test/web'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useApprovals } from '@/services/approval-monitor'
import { usePreferences } from '@/stores/preferences'
import { stubMatchMedia } from '@/test/web'
import {
  KEY,
  assistantRow,
  chatRpc,
  emit,
  openChat,
  resetChatState,
  userRow,
} from '@/test/web-chat'
import { INLINE_RESULT_LIMIT } from './transcript/ToolRow'
import { foldTurn } from './transcript/fold'
import type { Turn } from './model/types'

const originalMatchMedia = window.matchMedia
beforeEach(resetChatState)
afterEach(() => {
  window.matchMedia = originalMatchMedia
  vi.restoreAllMocks()
  vi.useRealTimers()
})

function log() {
  return screen.getByRole('log', { name: 'Conversation' })
}

const toolSegments = (n: number) =>
  Array.from({ length: n }, (_, i) => [
    { type: 'tool_use', tool_use_id: `t${i}`, name: 'web_search', input: { query: `q${i}` } },
    {
      type: 'tool_result',
      tool_use_id: `t${i}`,
      name: 'web_search',
      result: `r${i}`,
      is_error: false,
    },
  ]).flat()

describe('transcript layout', () => {
  it('shows the user message as a right-aligned bubble and the reply as prose', async () => {
    await openChat(chatRpc([userRow('u1', 'hello'), assistantRow('a1', 'Hi! How can I help?')]))
    const user = await within(log()).findByText('hello')
    expect(user.closest('[data-role]')).toHaveAttribute('data-role', 'user')
    expect(user.closest('[data-role="user"]')).toHaveClass('items-end')
    expect(within(log()).getByText('Hi! How can I help?').closest('[data-role]')).toHaveAttribute(
      'data-role',
      'assistant',
    )
    // The reply opens with the agent's identity line.
    const reply = within(log()).getByText('Hi! How can I help?').closest('[data-role]')!
    expect(reply.querySelector('.chat-ident')).toHaveTextContent(/main/i)
    // No source comment leaks into the page as text (a `//` line once sat in JSX).
    expect(log().parentElement!.parentElement!.textContent).not.toMatch(/\/\/|\/\*/)
  })

  it('copies a reply as Markdown with a confirmation', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    await openChat(chatRpc([userRow('u1', 'q'), assistantRow('a1', '**bold** answer')]))
    const reply = (await within(log()).findByText('bold')).closest(
      '[data-role="assistant"]',
    ) as HTMLElement
    await user.click(within(reply).getByRole('button', { name: 'Copy message as Markdown' }))
    expect(writeText).toHaveBeenCalledWith('**bold** answer')
    expect(await screen.findByText('Copied')).toBeInTheDocument()
  })
})

describe('Worked for fold', () => {
  const base = (patch: Partial<Turn>): Turn => ({
    id: 't',
    userMessage: null,
    items: [],
    status: 'done',
    startedAt: 0,
    endedAt: 42_000,
    ...patch,
  })
  const tool = (id: string, state: 'done' | 'failed' | 'running' = 'done') => ({
    kind: 'toolCall' as const,
    id,
    toolUseId: id,
    name: 'bash',
    isError: state === 'failed',
    truncated: false,
    state,
  })

  it('folds three tool calls behind "Worked for 42s · 3 tools" and keeps the answer visible', async () => {
    await openChat(
      chatRpc([
        userRow('u1', 'go', 1_700_000_000),
        assistantRow('a1', 'Done.', {
          timestamp: 1_700_000_042,
          tool_calls: [...toolSegments(3), { type: 'text', text: 'Done.' }],
        }),
      ]),
    )
    const fold = await within(log()).findByRole('button', { name: 'Worked for 42s · 3 tools' })
    expect(fold).toHaveAttribute('aria-expanded', 'false')
    expect(within(log()).getByText('Done.')).toBeVisible()
    expect(within(log()).queryByText('web_search')).not.toBeInTheDocument()
    await userEvent.setup().click(fold)
    expect(within(log()).getAllByText('web_search')).toHaveLength(3)
  })

  it('keeps a live turn expanded', () => {
    const f = foldTurn(
      base({ status: 'live', items: [tool('a'), { kind: 'text', id: 'x', text: 'partial' }] }),
    )
    expect(f.folded).toBe(false)
  })

  it('leaves a trailing failed tool row outside the fold', () => {
    const f = foldTurn(
      base({ items: [tool('a'), { kind: 'text', id: 'x', text: 'answer' }, tool('b', 'failed')] }),
    )
    expect(f.folded).toBe(true)
    expect(f.work.map((i) => i.id)).toEqual(['a'])
    expect(f.visible.map((i) => i.id)).toEqual(['x', 'b'])
  })

  it('stays expanded for a turn with no answer', () => {
    expect(foldTurn(base({ items: [tool('a')] })).folded).toBe(false)
  })
})

describe('tool rows', () => {
  it('expands a web_search row with Enter to show its arguments and result', async () => {
    const rpc = chatRpc([userRow('u1', 'search')])
    await openChat(rpc)
    emit(rpc, 'tool_use_start', { tool_use_id: 'w1', tool_name: 'web_search' })
    emit(rpc, 'tool_result', {
      tool_use_id: 'w1',
      tool_name: 'web_search',
      result: 'BTC is up',
      arguments: { query: 'btc news' },
    })
    const row = await within(log()).findByRole('button', { name: /web_search/ })
    act(() => row.focus())
    await userEvent.setup().keyboard('{Enter}')
    expect(row).toHaveAttribute('aria-expanded', 'true')
    const region = screen.getByRole('region', { name: 'web_search details' })
    expect(region).toHaveTextContent('btc news')
    expect(region).toHaveTextContent('BTC is up')
  })

  it('offers Open in side panel for long output and opens it there', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc([userRow('u1', 'run')])
    await openChat(rpc)
    const long = 'x'.repeat(INLINE_RESULT_LIMIT + 50)
    emit(rpc, 'tool_use_start', { tool_use_id: 'b1', tool_name: 'bash' })
    emit(rpc, 'tool_result', {
      tool_use_id: 'b1',
      tool_name: 'bash',
      result: long,
      arguments: { command: 'cat big' },
    })
    await user.click(await within(log()).findByRole('button', { name: /bash/ }))
    await user.click(screen.getByRole('button', { name: 'Open in side panel' }))
    const panel = screen.getByRole('complementary', { name: 'Side panel' })
    expect(within(panel).getByRole('tabpanel')).toHaveTextContent(long)
  })

  it('marks a failed call with an icon and the word Failed', async () => {
    const rpc = chatRpc([userRow('u1', 'run')])
    await openChat(rpc)
    emit(rpc, 'tool_use_start', { tool_use_id: 'f1', tool_name: 'bash' })
    emit(rpc, 'tool_result', {
      tool_use_id: 'f1',
      tool_name: 'bash',
      result: 'boom',
      is_error: true,
    })
    const row = (await within(log()).findByRole('button', { name: /bash/ })).closest(
      '[data-tool-state]',
    )!
    expect(row).toHaveAttribute('data-tool-state', 'failed')
    expect(row).toHaveTextContent('Failed')
    expect(row.querySelector('[data-status-icon]')).not.toBeNull()
  })
})

describe('thinking', () => {
  const rows = [userRow('u1', 'why'), assistantRow('a1', 'Because.', { has_thinking: true })]

  it('loads the reasoning with chat.thinking on first expand', async () => {
    const rpc = chatRpc(rows).handle('chat.thinking', { reasoning: 'Deep thoughts' })
    await openChat(rpc)
    const user = userEvent.setup()
    // Reasoning folds into the completed turn's "Worked for" disclosure.
    await user.click(await within(log()).findByRole('button', { name: /Worked for/ }))
    await user.click(within(log()).getByRole('button', { name: 'Thinking' }))
    expect(await within(log()).findByText('Deep thoughts')).toBeInTheDocument()
    expect(rpc.callsTo('chat.thinking')).toEqual([{ sessionKey: KEY, messageId: 'a1' }])
  })

  it('shows no Thinking block when Show thinking is off', async () => {
    usePreferences.setState({ showThinking: false })
    await openChat(chatRpc(rows))
    await within(log()).findByText('Because.')
    expect(within(log()).queryByRole('button', { name: 'Thinking' })).not.toBeInTheDocument()
  })
})

describe('header', () => {
  it('renames the session in place', async () => {
    const user = userEvent.setup()
    let name = 'Test chat'
    const rpc = chatRpc()
      .handle('sessions.list', () => ({
        sessions: [{ key: KEY, display_name: name, updated_at: Date.now() / 1000 }],
      }))
      .handle('sessions.rename', (p: Record<string, unknown>) => {
        name = String(p.name)
        return { ok: true }
      })
    await openChat(rpc)
    await user.click(await screen.findByRole('button', { name: 'Test chat' }))
    const input = screen.getByRole('textbox', { name: 'Rename session' })
    await user.clear(input)
    await user.type(input, 'Q3 research{Enter}')
    await waitFor(() =>
      expect(rpc.callsTo('sessions.rename')).toEqual([{ key: KEY, name: 'Q3 research' }]),
    )
    expect(
      await screen.findByRole('heading', { name: 'Q3 research', level: 1 }),
    ).toBeInTheDocument()
  })

  it('compacts from the ⋯ menu and shows the divider when it finishes', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc([userRow('u1', 'a'), assistantRow('a1', 'b')])
    await openChat(rpc)
    await user.click(screen.getByRole('button', { name: 'Session actions' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Compact' }))
    expect(rpc.callsTo('sessions.contextCompact')).toEqual([{ key: KEY }])
    emit(rpc, 'compaction', { status: 'completed', summary: 's' })
    expect(
      await within(log()).findByRole('separator', { name: 'Earlier messages were compacted' }),
    ).toBeInTheDocument()
  })
})

describe('session actions menu', () => {
  it('gives every action an icon, so the labels line up', async () => {
    const user = userEvent.setup()
    await openChat(chatRpc())
    await user.click(screen.getByRole('button', { name: 'Session actions' }))
    const items = await screen.findAllByRole('menuitem')
    expect(items.map((item) => item.textContent)).toEqual([
      'Rename',
      'Compact',
      'Reset session',
      'Export as Markdown',
      'Copy session key',
    ])
    for (const item of items) expect(item.querySelector('svg')).not.toBeNull()
  })
})

describe('composer', () => {
  it('marks focus with a ring as well as the border (the border alone is ~1.6:1 in light)', async () => {
    const { composer } = await openChat(chatRpc())
    expect(composer.closest('.chat-composer')).not.toBeNull()
    // The focus chrome lives in chat.css: the accent border plus a 1px ring.
    const css = readFileSync('src/web/styles/chat.css', 'utf8')
    const rule = /\.chat-composer:focus-within\s*\{([^}]*)\}/.exec(css)?.[1] ?? ''
    expect(rule).toMatch(/border-color:\s*var\(--ring\)/)
    expect(rule).toMatch(/0 0 0 1px var\(--ring\)/)
    expect(rule).toMatch(/var\(--focus-glow\)/)
    // No resting composer shadow (a theme override, say) may out-rank the
    // focus rule: that is how light once lost its ring and glow.
    const specificity = (selector: string) =>
      (selector.replace(/:where\([^)]*\)/g, '').match(/\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+/g) ?? [])
        .length
    const focus = specificity('.chat-composer:focus-within')
    const resting = [
      ...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g),
    ].filter(
      ([, selector, body]) =>
        /\.chat-composer(?![\w-])/.test(selector!) &&
        !/:focus-within|:hover|data-dragging/.test(selector!) &&
        /box-shadow/.test(body!),
    )
    expect(resting.length).toBeGreaterThan(0)
    for (const [, selector] of resting) {
      expect(specificity(selector!.trim()), selector).toBeLessThanOrEqual(focus)
    }
  })

  it('Enter mode: Shift+Enter adds a line and nothing is sent', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc()
    const { composer } = await openChat(rpc)
    await user.type(composer, 'line one{Shift>}{Enter}{/Shift}line two')
    expect(composer).toHaveValue('line one\nline two')
    expect(rpc.callsTo('chat.send')).toEqual([])
    await user.keyboard('{Enter}')
    await waitFor(() =>
      expect(rpc.callsTo('chat.send')[0]).toMatchObject({ message: 'line one\nline two' }),
    )
  })

  it('Mod+Enter mode: Enter adds a line and Ctrl+Enter sends', async () => {
    usePreferences.setState({ sendShortcut: 'mod-enter' })
    const user = userEvent.setup()
    const rpc = chatRpc()
    const { composer } = await openChat(rpc)
    await user.type(composer, 'a{Enter}b')
    expect(composer).toHaveValue('a\nb')
    expect(rpc.callsTo('chat.send')).toEqual([])
    await user.keyboard('{Control>}{Enter}{/Control}')
    await waitFor(() => expect(rpc.callsTo('chat.send')).toHaveLength(1))
  })

  it('never sends on an Enter that confirms an IME composition', async () => {
    const rpc = chatRpc()
    const { composer } = await openChat(rpc)
    fireEvent.change(composer, { target: { value: 'xin chào' } })
    fireEvent.keyDown(composer, { key: 'Enter', isComposing: true, keyCode: 229 })
    expect(rpc.callsTo('chat.send')).toEqual([])
    expect(composer).toHaveValue('xin chào')
  })

  it('treats Return as a new line on a phone; only the Send button sends', async () => {
    stubMatchMedia(
      (q) =>
        q.includes('pointer: coarse') ||
        q.includes('max-width: 768px') ||
        q.includes('reduced-motion'),
    )
    const user = userEvent.setup()
    const rpc = chatRpc()
    const { composer } = await openChat(rpc)
    await user.type(composer, 'hi{Enter}there')
    expect(composer).toHaveValue('hi\nthere')
    expect(rpc.callsTo('chat.send')).toEqual([])
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(rpc.callsTo('chat.send')).toHaveLength(1))
  })

  it('keeps a draft per session', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc()
    const { composer, router } = await openChat(rpc)
    await user.type(composer, 'draft for A')
    await act(() => router.navigate('/chat/main/webchat/other'))
    const other = await screen.findByRole('textbox', { name: 'Message' })
    await waitFor(() => expect(other).toHaveValue(''))
    await act(() => router.navigate('/chat/main/webchat/t1'))
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('draft for A'),
    )
  })

  it('shows a pasted image in the tray with a remove action', async () => {
    const rpc = chatRpc()
    const { composer } = await openChat(rpc)
    const file = new File([new Uint8Array([137, 80, 78, 71])], 'shot.png', { type: 'image/png' })
    fireEvent.paste(composer, { clipboardData: { files: [file], types: ['Files'] } })
    expect(await screen.findByRole('button', { name: 'Remove shot.png' })).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Remove shot.png' }))
    expect(screen.queryByText('shot.png')).not.toBeInTheDocument()
  })

  it('disables Send with a reason while an attachment uploads', async () => {
    let finish: (v: { file_uuid: string }) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise((resolve) => (finish = () => resolve(new Response('{"file_uuid":"f1"}')))),
      ),
    )
    const rpc = chatRpc()
    const { composer } = await openChat(rpc)
    const big = new File([new Uint8Array(3 * 1024 * 1024)], 'report.pdf', {
      type: 'application/pdf',
    })
    fireEvent.paste(composer, { clipboardData: { files: [big], types: ['Files'] } })
    fireEvent.change(composer, { target: { value: 'see this' } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled())
    expect(screen.getByText('Waiting for attachments to finish uploading')).toBeInTheDocument()
    void finish
    vi.unstubAllGlobals()
  })

  it('keeps a reader at the end of a settled transcript there while the draft grows', async () => {
    // The browser defect: sizing the input sets `height: auto` and reads
    // scrollHeight, a forced layout in which the input is one row tall. If the
    // composer box shrinks with it, the transcript gets taller for that
    // layout and clamps its scrollTop; the restored height then leaves the
    // reader above the end, the scroll up reads as the reader and pauses
    // following. A small layout model: the transcript fills what the
    // composer leaves, scrollTop is clamped at every layout.
    const observers: Array<{ cb: ResizeObserverCallback; targets: Element[] }> = []
    vi.stubGlobal(
      'ResizeObserver',
      class {
        targets: Element[] = []
        constructor(public cb: ResizeObserverCallback) {
          observers.push(this)
        }
        observe(target: Element) {
          this.targets.push(target)
        }
        unobserve() {}
        disconnect() {
          this.targets = []
        }
      },
    )
    const { composer } = await openChat(chatRpc())
    const scroller = log()
    const box = composer.parentElement!
    const ROW = 24
    const VIEW = 700
    const CONTENT = 2000
    const inputHeight = () => {
      const h = parseFloat(composer.style.height)
      return Number.isNaN(h) ? ROW : h
    }
    const boxHeight = () => Math.max(parseFloat(box.style.minHeight) || 0, inputHeight() + 60)
    const viewHeight = () => VIEW - boxHeight()
    let top = 0
    let scrolled = false
    const layout = () => {
      const clamped = Math.max(0, Math.min(top, CONTENT - viewHeight()))
      if (clamped !== top) scrolled = true
      top = clamped
    }
    Object.defineProperties(scroller, {
      scrollHeight: { configurable: true, get: () => CONTENT },
      clientHeight: { configurable: true, get: () => viewHeight() },
      scrollTop: {
        configurable: true,
        get: () => top,
        set: (v: number) => {
          top = v
          layout()
          scrolled = true
        },
      },
    })
    scroller.scrollTo = ((opts: ScrollToOptions) => {
      scroller.scrollTop = opts.top ?? 0
    }) as typeof scroller.scrollTo
    Object.defineProperty(box, 'offsetHeight', { configurable: true, get: boxHeight })
    Object.defineProperty(composer, 'scrollHeight', {
      configurable: true,
      get: () => {
        layout()
        return (composer as HTMLTextAreaElement).value.split('\n').length * ROW
      },
    })
    // A rendering step: scroll events first, then resize observations.
    let lastView = viewHeight()
    const frame = () =>
      act(() => {
        layout()
        if (scrolled) {
          scrolled = false
          scroller.dispatchEvent(new Event('scroll'))
        }
        if (viewHeight() !== lastView) {
          lastView = viewHeight()
          for (const o of observers)
            if (o.targets.includes(scroller)) o.cb([], o as unknown as ResizeObserver)
        }
      })
    const distance = () => CONTENT - top - viewHeight()
    act(() => scroller.scrollTo({ top: CONTENT }))
    frame()
    expect(distance()).toBe(0)
    let draft = ''
    for (let i = 0; i < 8; i++) {
      draft += `${i ? '\n' : ''}line ${i}`
      fireEvent.change(composer, { target: { value: draft } })
      frame()
      expect(distance(), `after line ${i + 1}`).toBe(0)
    }
    expect(viewHeight()).toBe(VIEW - 8 * ROW - 60)
    expect(document.querySelector('.chat-latest')).toHaveAttribute('data-show', 'false')
    vi.unstubAllGlobals()
  })

  it('publishes its height for the approval tray, and takes it back on leaving', async () => {
    const { unmount } = await openChat(chatRpc())
    const root = document.documentElement
    await waitFor(() => expect(root.style.getPropertyValue('--tray-clearance')).toMatch(/^\d+px$/))
    unmount()
    expect(root.style.getPropertyValue('--tray-clearance')).toBe('')
  })

  it('checks the current route in the route picker', async () => {
    const user = userEvent.setup()
    await openChat(chatRpc())
    await user.click(screen.getByRole('button', { name: /^Model route:/ }))
    const items = await screen.findAllByRole('menuitemcheckbox')
    const checked = items.filter((item) => item.getAttribute('aria-checked') === 'true')
    expect(checked).toHaveLength(1)
    // The state is visible, not only announced: a check mark on that row alone.
    expect(checked[0]!.querySelector('svg')).not.toBeNull()
    for (const item of items.filter((i) => i !== checked[0])) {
      expect(item.querySelector('svg')).toBeNull()
    }
  })

  it('pins a named model in the route picker', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc()
    await openChat(rpc)
    await user.click(screen.getByRole('button', { name: /^Model route:/ }))
    await user.click(await screen.findByRole('menuitemcheckbox', { name: 'Claude Opus' }))
    await waitFor(() =>
      expect(rpc.callsTo('router.hold.set')).toContainEqual(
        expect.objectContaining({ key: KEY, model: 'anthropic/claude-opus' }),
      ),
    )
  })

  it('filters slash commands and runs /compact with Enter', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc()
    const { composer } = await openChat(rpc)
    await user.type(composer, '/comp')
    const menu = await screen.findByRole('listbox', { name: 'Slash commands' })
    expect(
      within(menu)
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual([expect.stringContaining('/compact')])
    await user.keyboard('{Enter}')
    await waitFor(() => expect(rpc.callsTo('sessions.contextCompact')).toEqual([{ key: KEY }]))
  })
})

describe('send, stop and queue', () => {
  it('shows Stop while running with an empty composer and aborts', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc([userRow('u1', 'go')])
    await openChat(rpc)
    emit(rpc, 'text_delta', { text: 'working' })
    await user.click(await screen.findByRole('button', { name: 'Stop' }))
    expect(rpc.callsTo('chat.abort')).toEqual([{ sessionKey: KEY, source: 'web' }])
  })

  it('queues a follow-up while running and sends it after the turn ends', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc([userRow('u1', 'go')])
    const { composer } = await openChat(rpc)
    emit(rpc, 'text_delta', { text: 'working' })
    await user.type(composer, 'also check BTC')
    await user.click(screen.getByRole('button', { name: 'Queue' }))
    const queue = screen.getByRole('region', { name: 'Queued messages' })
    expect(within(queue).getByText('also check BTC')).toBeInTheDocument()
    expect(rpc.callsTo('chat.send')).toEqual([])
    emit(rpc, 'done', { text: 'working', outcome: { kind: 'completed' } })
    await waitFor(
      () => expect(rpc.callsTo('chat.send')[0]).toMatchObject({ message: 'also check BTC' }),
      {
        timeout: 3000,
      },
    )
  })
})

describe('failed turns', () => {
  it('shows the reason with Try again (fresh idempotency key) and Edit message', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc()
    const { composer } = await openChat(rpc)
    await user.type(composer, 'explain{Enter}')
    await waitFor(() => expect(rpc.callsTo('chat.send')).toHaveLength(1))
    emit(rpc, 'error', { message: 'Provider overloaded', code: 'provider_error' })
    expect(
      await within(log()).findByText('This turn failed: Provider overloaded'),
    ).toBeInTheDocument()
    await user.click(within(log()).getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(rpc.callsTo('chat.send')).toHaveLength(2))
    const [first, second] = rpc.callsTo('chat.send')
    expect(second!.message).toBe('explain')
    expect(second!.idempotency_key).not.toBe(first!.idempotency_key)
    await user.click(within(log()).getAllByRole('button', { name: 'Edit message' })[0]!)
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('explain')
  })
})

describe('approvals in the open session', () => {
  it('docks the approval above the composer and shows Waiting for approval', async () => {
    const rpc = chatRpc([userRow('u1', 'clean up')])
    await openChat(rpc)
    emit(rpc, 'tool_use_start', { tool_use_id: 'b', tool_name: 'bash' })
    act(() =>
      useApprovals.getState().upsert({
        id: 'ap',
        namespace: 'exec',
        toolName: 'bash',
        sessionKey: KEY,
        command: 'rm -rf build',
      }),
    )
    const dock = await screen.findByRole('region', { name: 'Approval needed: bash' })
    expect(within(dock).getByText('rm -rf build')).toBeInTheDocument()
    expect(within(dock).getByRole('button', { name: 'Deny' })).toBeInTheDocument()
    expect(within(log()).getByText('Waiting for approval')).toBeInTheDocument()
    expect(
      screen.queryByRole('complementary', { name: 'Pending approvals' }),
    ).not.toBeInTheDocument()
  })
})

describe('Details', () => {
  it('docks on a wide pane with the session context, and an explicit close sticks', async () => {
    const user = userEvent.setup()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1600 })
    const rpc = chatRpc([userRow('u1', 'a'), assistantRow('a1', 'b')], { project_id: 'p1' })
    await openChat(rpc)
    const details = await screen.findByRole('complementary', { name: 'Session details' })
    expect(within(details).getByText('Agent').nextSibling).toHaveTextContent('main')
    expect(within(details).getByText('Project').nextSibling).toHaveTextContent('Alpha')
    expect(within(details).getByText('Queued messages')).toBeInTheDocument()
    await user.click(within(details).getByRole('button', { name: 'Close details' }))
    expect(screen.queryByRole('complementary', { name: 'Session details' })).not.toBeInTheDocument()
    emit(rpc, 'text_delta', { text: 'new message' })
    expect(screen.queryByRole('complementary', { name: 'Session details' })).not.toBeInTheDocument()
  })

  it('opens as an overlay on a narrow pane', async () => {
    const user = userEvent.setup()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 900 })
    await openChat()
    expect(screen.queryByRole('complementary', { name: 'Session details' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Details' }))
    const sheet = await screen.findByRole('dialog', { name: 'Session details' })
    expect(
      within(sheet).getByRole('complementary', { name: 'Session details' }),
    ).toBeInTheDocument()
  })
})

describe('side panel', () => {
  it('opens a document artifact in a tab and keeps the draft', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc([
      userRow('u1', 'report'),
      assistantRow('a1', 'Here.', {
        artifacts: [
          {
            id: 'd1',
            name: 'report.md',
            mime: 'text/markdown',
            size: 100,
            download_url: '/api/v1/artifacts/d1',
          },
        ],
      }),
    ])
    const { composer } = await openChat(rpc)
    await user.type(composer, 'my draft')
    await user.click(await within(log()).findByRole('button', { name: 'Open report.md' }))
    const panel = screen.getByRole('complementary', { name: 'Side panel' })
    expect(within(panel).getByRole('tab', { name: 'report.md' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(composer).toHaveValue('my draft')
    await user.keyboard('{Escape}')
  })
})

describe('cards', () => {
  it('answers an ask_user question in place', async () => {
    const user = userEvent.setup()
    const result = JSON.stringify({
      status: 'question_presented',
      questions: [{ question: 'Which day?', options: [{ label: 'Mon' }, { label: 'Tue' }] }],
    })
    const rpc = chatRpc([
      userRow('u1', 'book'),
      assistantRow('a1', '', {
        tool_calls: [
          { type: 'tool_use', tool_use_id: 'q', name: 'ask_user', input: {} },
          { type: 'tool_result', tool_use_id: 'q', name: 'ask_user', result, is_error: false },
        ],
      }),
    ])
    await openChat(rpc)
    await user.click(await within(log()).findByRole('radio', { name: 'Tue' }))
    await user.click(within(log()).getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(rpc.callsTo('chat.send')[0]!.message).toContain('Tue'))
    expect(within(log()).getAllByText('Answered').length).toBeGreaterThan(0)
  })

  it('approves a plan through plan.mode.set and sends the approval', async () => {
    const user = userEvent.setup()
    const result = JSON.stringify({ status: 'presented', plan: '1. Do it' })
    const rpc = chatRpc([
      userRow('u1', 'plan'),
      assistantRow('a1', '', {
        tool_calls: [
          { type: 'tool_use', tool_use_id: 'p', name: 'exit_plan_mode', input: {} },
          {
            type: 'tool_result',
            tool_use_id: 'p',
            name: 'exit_plan_mode',
            result,
            is_error: false,
          },
        ],
      }),
    ])
    await openChat(rpc)
    await user.click(await within(log()).findByRole('button', { name: 'Approve plan' }))
    await waitFor(() => expect(rpc.callsTo('plan.mode.set')).toEqual([{ key: KEY, mode: 'off' }]))
    await waitFor(() => expect(rpc.callsTo('chat.send')[0]!.message).toMatch(/Plan approved/))
  })

  it('cancels an ACP coding-agent run', async () => {
    const user = userEvent.setup()
    const rpc = chatRpc([
      userRow('u1', 'code'),
      assistantRow('a1', 'Spawned.', {
        tool_calls: [
          {
            type: 'tool_use',
            tool_use_id: 's',
            name: 'sessions_spawn',
            input: { runtime: 'acp', agent_id: 'codex' },
          },
          {
            type: 'tool_result',
            tool_use_id: 's',
            name: 'sessions_spawn',
            result: JSON.stringify({ session_key: 'agent:codex:acp:1' }),
            is_error: false,
          },
          { type: 'text', text: 'Spawned.' },
        ],
      }),
    ]).handle('acp.cancel', { ok: true })
    await openChat(rpc)
    await user.click(await within(log()).findByRole('button', { name: /Worked for/ }))
    await user.click(within(log()).getByRole('button', { name: 'Cancel' }))
    expect(rpc.callsTo('acp.cancel')).toEqual([{ session_key: 'agent:codex:acp:1' }])
  })
})
