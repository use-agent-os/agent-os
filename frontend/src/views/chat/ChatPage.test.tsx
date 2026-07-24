import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useEffect } from 'react'
import { MemoryRouter, useLocation } from 'react-router'
import { focusManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { ChatPage } from './ChatPage'
import * as logicModule from './logic'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

function makeImageFile(name: string, size = 100): File {
  const file = new File([new Blob(['img'], { type: 'image/png' })], name, { type: 'image/png' })
  Object.defineProperty(file, 'size', { value: size, configurable: true })
  return file
}

// Same provider-wrapping pattern SkillsPage.test.tsx uses: there is no shared
// `@/test/utils` wrapper in this repo, so the RPC provider is stubbed via a
// module mock and the tree is wrapped in MemoryRouter + QueryClientProvider.
type Handler = (...args: unknown[]) => void
const SLASH_CATALOG = [
  {
    name: '/help',
    usage: '/help',
    description: 'Show the command list',
    aliases: [],
    execution: { action: '/help' },
  },
  {
    name: '/reset',
    usage: '/reset',
    description: 'Reset the session',
    aliases: [],
    execution: { action: 'reset_session' },
  },
]

function makeRpc() {
  const listeners = new Map<string, Set<Handler>>()
  return {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn((...args: unknown[]) => {
      if (args[0] === 'commands.list_for_surface') {
        return Promise.resolve({ surface: 'web_chat', commands: SLASH_CATALOG })
      }
      return Promise.resolve({})
    }),
    on: vi.fn((event: string, handler: Handler) => {
      if (!listeners.has(event)) listeners.set(event, new Set())
      listeners.get(event)!.add(handler)
      return () => listeners.get(event)?.delete(handler)
    }),
    emit(event: string, ...args: unknown[]) {
      listeners.get(event)?.forEach((h) => h(...args))
      // The real RPC also fans every frame out to `*` wildcard listeners (the
      // transcript's terminal-event backstop registers there, chat.js:4965).
      listeners.get('*')?.forEach((h) => h(event, ...args))
    },
  }
}
let mockRpc = makeRpc()

vi.mock('@/app/providers', () => ({
  useRpc: () => mockRpc,
  useBootstrap: () => ({
    version: '1',
    ws_url: 'ws://127.0.0.1:18791/ws',
    auth_mode: 'none',
    base_path: '/control',
    config_path: '/tmp/agentos.toml',
    features: {},
  }),
}))

// A location probe so tests can assert the URL `?session=` after a switch. Held
// on a mutable object (not a reassigned module `let`) so the effect write is a
// property mutation, which the react-hooks lint rules permit.
const probe = { search: '' }
function LocationProbe() {
  const loc = useLocation()
  useEffect(() => {
    probe.search = loc.search
  }, [loc.search])
  return null
}

function renderPage(initialEntry = '/chat') {
  probe.search = ''
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <ChatPage />
        <LocationProbe />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

async function clickChatAction(name: string) {
  const trigger = screen.getByRole('button', { name: 'Chat actions' })
  if (trigger.getAttribute('aria-expanded') !== 'true') fireEvent.click(trigger)
  fireEvent.click(await screen.findByRole('menuitem', { name }))
}

beforeEach(() => {
  // The SessionChip fetches /api/sessions on open (chat.js:2026). Stub a default
  // OK response; individual tests override as needed.
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        sessions: ['agent:main:webchat:default', 'agent:main:webchat:other'],
      }),
    }),
  )
  try {
    localStorage.clear()
  } catch {
    /* ignore */
  }
})

afterEach(() => {
  focusManager.setFocused(undefined)
  vi.unstubAllGlobals()
})

describe('ChatPage', () => {
  it('renders the full-bleed chat shell with a thread region', () => {
    mockRpc = makeRpc()
    renderPage()
    expect(document.querySelector('.chat-thread')).toHaveAttribute('data-history-ready', 'false')
    expect(screen.getByRole('status')).toHaveTextContent('Opening conversation…')
    expect(screen.getByRole('heading', { name: 'Chat', level: 1 })).toBeInTheDocument()
    expect(document.title).toBe('Chat - AgentOS Control')
  })

  it('mounts the thread region above a composer row', () => {
    mockRpc = makeRpc()
    renderPage()
    expect(document.querySelector('.chat-stage')).not.toBeNull()
    expect(document.querySelector('.chat-composer')).not.toBeNull()
    // The rendered .router-fx receipt owns aria-live. The mount point must stay
    // neutral so assistive technology does not announce the same update twice.
    expect(document.querySelector('#chat-routerfx-dock')).not.toHaveAttribute('aria-live')
  })

  it('preloads the web search provider badge seed on view entry', async () => {
    mockRpc = makeRpc()
    renderPage()

    await waitFor(() => {
      expect(mockRpc.call).toHaveBeenCalledWith('tools.search_provider', {})
    })
  })

  it('reconstructs persisted tools, attachments, artifacts, usage, and router receipt', async () => {
    mockRpc = makeRpc()
    mockRpc.call.mockImplementation((...args: unknown[]) => {
      if (args[0] === 'commands.list_for_surface') {
        return Promise.resolve({ surface: 'web_chat', commands: SLASH_CATALOG })
      }
      if (args[0] === 'config.get') {
        return Promise.resolve({
          agentos_router: {
            enabled: true,
            rollout_phase: 'full',
            tiers: {
              c1: { model: 'provider/fast', supports_image: true },
              c2: { model: 'provider/flagship', supports_image: true },
            },
          },
        })
      }
      if (args[0] === 'chat.history') {
        return Promise.resolve({
          messages: [
            {
              role: 'user',
              text: 'make a chart',
              timestamp: 100,
              attachments: [{ name: 'input.png', mime: 'image/png', data: 'AA==' }],
            },
            {
              role: 'assistant',
              text: 'Done',
              timestamp: 200,
              tool_calls: [
                {
                  type: 'tool_use',
                  name: 'publish_artifact',
                  tool_use_id: 'tool-1',
                  input: { name: 'AGENTOS_7day_chart.png' },
                },
                {
                  type: 'tool_result',
                  tool_use_id: 'tool-1',
                  content: 'published',
                },
              ],
              artifacts: [
                {
                  id: 'artifact-1',
                  name: 'AGENTOS_7day_chart.png',
                  mime: 'image/png',
                  download_url: '/api/v1/artifacts/artifact-1',
                },
                {
                  id: 'artifact-2',
                  name: 'voice-note.wav',
                  mime: 'audio/wav',
                  download_url: '/api/v1/artifacts/artifact-2',
                },
              ],
              usage: {
                model: 'provider/fast',
                routed_model: 'provider/fast',
                routed_tier: 'c1',
                routing_source: 'pilot',
                total_savings_pct: 42,
                input_tokens: 100,
                output_tokens: 20,
              },
            },
          ],
          history_scope: 'complete',
        })
      }
      return Promise.resolve({})
    })
    renderPage()

    await waitFor(() => {
      expect(document.querySelector('.msg-artifact-card--image')).not.toBeNull()
      expect(document.querySelector('.chat-tools-collapse')).not.toBeNull()
      expect(document.querySelector('.msg-thumb')).not.toBeNull()
      expect(document.querySelector('.msg-meta')).toHaveTextContent('fast↑100 ↓20')
      expect(document.querySelector('.msg-meta__saved')).toBeNull()
      expect(document.querySelector('.msg-meta__combo')).toBeNull()
      expect(document.querySelector('#chat-routerfx-dock .router-fx')).toHaveAttribute(
        'data-state',
        'settled',
      )
    })
    expect(document.querySelector('.chat-thread')).toHaveAttribute('data-history-ready', 'true')
    expect(document.querySelector('.msg-artifact-card__name')).toHaveTextContent(
      'AGENTOS_7day_chart.png',
    )
    const audioCard = document.querySelector('.msg-artifact-card--audio') as HTMLElement
    expect(audioCard).not.toHaveAttribute('data-artifact-download')
    expect(audioCard.querySelector('a[data-artifact-download]')).toHaveAttribute(
      'data-artifact-download',
      '/api/v1/artifacts/artifact-2',
    )
    const fetchCallsBeforeAudioClick = (fetch as ReturnType<typeof vi.fn>).mock.calls.length
    fireEvent.click(audioCard.querySelector('audio') as HTMLAudioElement)
    expect(fetch).toHaveBeenCalledTimes(fetchCallsBeforeAudioClick)
  })

  it('reveals entry atomically after replay and its terminal history refresh settle', async () => {
    mockRpc = makeRpc()
    let resolveSubscribe!: (value: Record<string, unknown>) => void
    let resolveRefreshedHistory!: (value: Record<string, unknown>) => void
    const subscribeResult = new Promise<Record<string, unknown>>((resolve) => {
      resolveSubscribe = resolve
    })
    const refreshedHistory = new Promise<Record<string, unknown>>((resolve) => {
      resolveRefreshedHistory = resolve
    })
    let historyReads = 0
    mockRpc.call.mockImplementation((...args: unknown[]) => {
      if (args[0] === 'commands.list_for_surface') {
        return Promise.resolve({ surface: 'web_chat', commands: SLASH_CATALOG })
      }
      if (args[0] === 'chat.history') {
        historyReads += 1
        if (historyReads === 1) {
          return Promise.resolve({
            messages: [{ role: 'user', text: 'question', timestamp: 100 }],
            history_scope: 'complete',
          })
        }
        return refreshedHistory
      }
      if (args[0] === 'sessions.messages.subscribe') return subscribeResult
      return Promise.resolve({})
    })
    renderPage()

    await waitFor(() => expect(document.querySelector('.msg.user')).toHaveTextContent('question'))
    const thread = document.querySelector('.chat-thread') as HTMLElement
    expect(thread).toHaveAttribute('data-history-ready', 'false')

    act(() => {
      mockRpc.emit(
        'session.event.text_delta',
        {
          key: 'agent:main:webchat:default',
          stream_seq: 1,
          text: 'replayed draft',
        },
        { replayed: true },
      )
      mockRpc.emit(
        'chat.done',
        {
          key: 'agent:main:webchat:default',
          text: 'final answer',
        },
        { replayed: true },
      )
    })
    await waitFor(() => expect(historyReads).toBe(2))

    await act(async () => {
      resolveSubscribe({ subscribed: true, replay_complete: true, replayed_count: 2 })
      await subscribeResult
    })
    expect(thread).toHaveAttribute('data-history-ready', 'false')

    await act(async () => {
      resolveRefreshedHistory({
        messages: [
          { role: 'user', text: 'question', timestamp: 100 },
          { role: 'assistant', text: 'final answer', timestamp: 200 },
        ],
        history_scope: 'complete',
      })
      await refreshedHistory
    })
    await waitFor(() => expect(thread).toHaveAttribute('data-history-ready', 'true'))
    expect(thread.querySelector('.msg.assistant')).toHaveTextContent('final answer')
  })

  it('downloads a non-anchor artifact target through the authenticated delegate', async () => {
    mockRpc = makeRpc()
    renderPage()
    const blob = new Blob(['audio'], { type: 'audio/wav' })
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: async () => blob })
    vi.stubGlobal('fetch', fetchMock)
    const createObjectURL = vi.fn().mockReturnValue('blob:artifact')
    const revokeObjectURL = vi.fn()
    ;(URL as unknown as { createObjectURL: unknown }).createObjectURL = createObjectURL
    ;(URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = revokeObjectURL
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const thread = document.querySelector('.chat-thread') as HTMLElement
    thread.insertAdjacentHTML(
      'beforeend',
      '<button data-artifact-id="audio-1" data-artifact-name="clip.wav" data-artifact-download="/api/v1/artifacts/audio-1"><span>clip.wav</span></button>',
    )

    fireEvent.click(screen.getByText('clip.wav'))

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/v1/artifacts/audio-1'),
        expect.objectContaining({
          method: 'GET',
          credentials: 'same-origin',
          headers: expect.objectContaining({
            'x-agentos-session-key': 'agent:main:webchat:default',
          }),
        }),
      ),
    )
    expect(clickSpy).toHaveBeenCalledTimes(1)
    expect(createObjectURL).toHaveBeenCalledWith(blob)
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:artifact')
    clickSpy.mockRestore()
  })

  it('waits for router config before requesting the initial persisted history', async () => {
    mockRpc = makeRpc()
    let resolveConfig!: (value: Record<string, unknown>) => void
    const configResponse = new Promise<Record<string, unknown>>((resolve) => {
      resolveConfig = resolve
    })
    mockRpc.call.mockImplementation((...args: unknown[]) => {
      if (args[0] === 'commands.list_for_surface') {
        return Promise.resolve({ surface: 'web_chat', commands: SLASH_CATALOG })
      }
      if (args[0] === 'config.get') return configResponse
      return Promise.resolve({})
    })
    renderPage()

    await waitFor(() => {
      expect(mockRpc.call.mock.calls.some((call) => call[0] === 'config.get')).toBe(true)
    })
    expect(mockRpc.call.mock.calls.some((call) => call[0] === 'chat.history')).toBe(false)

    await act(async () => {
      resolveConfig({ agentos_router: { enabled: false, tiers: {} } })
      await configResponse
    })
    await waitFor(() => {
      expect(mockRpc.call.mock.calls.some((call) => call[0] === 'chat.history')).toBe(true)
    })
  })

  it('rebuilds persisted router receipts when initial config arrives after the wait ceiling', async () => {
    mockRpc = makeRpc()
    let resolveConfig!: (value: Record<string, unknown>) => void
    const configResponse = new Promise<Record<string, unknown>>((resolve) => {
      resolveConfig = resolve
    })
    const persistedHistory = {
      messages: [
        { role: 'user', text: 'route this', timestamp: 100 },
        {
          role: 'assistant',
          text: 'Done',
          timestamp: 200,
          usage: {
            model: 'provider/fast',
            routed_model: 'provider/fast',
            routed_tier: 'c1',
            routing_source: 'pilot',
          },
        },
      ],
      history_scope: 'complete',
    }
    mockRpc.call.mockImplementation((...args: unknown[]) => {
      if (args[0] === 'commands.list_for_surface') {
        return Promise.resolve({ surface: 'web_chat', commands: SLASH_CATALOG })
      }
      if (args[0] === 'config.get') return configResponse
      if (args[0] === 'chat.history') return Promise.resolve(persistedHistory)
      return Promise.resolve({})
    })
    renderPage()

    await waitFor(
      () => {
        expect(mockRpc.call.mock.calls.filter((call) => call[0] === 'chat.history')).toHaveLength(1)
      },
      { timeout: 2500 },
    )
    expect(document.querySelector('#chat-routerfx-dock .router-fx')).toBeNull()

    await act(async () => {
      resolveConfig({
        agentos_router: {
          enabled: true,
          rollout_phase: 'full',
          tiers: {
            c1: { model: 'provider/fast', supports_image: true },
            c2: { model: 'provider/flagship', supports_image: true },
          },
        },
      })
      await configResponse
    })

    await waitFor(() => {
      expect(mockRpc.call.mock.calls.filter((call) => call[0] === 'chat.history')).toHaveLength(2)
      expect(document.querySelector('#chat-routerfx-dock .router-fx')).toHaveAttribute(
        'data-state',
        'settled',
      )
    })
  })

  it('shares history and live header grouping without inserting a day separator mid-turn', async () => {
    mockRpc = makeRpc()
    const today = new Date().toISOString().slice(0, 10)
    mockRpc.call.mockImplementation((...args: unknown[]) => {
      if (args[0] === 'commands.list_for_surface') {
        return Promise.resolve({ surface: 'web_chat', commands: SLASH_CATALOG })
      }
      if (args[0] === 'config.get') {
        return Promise.resolve({ agentos_router: { enabled: false, tiers: {} } })
      }
      if (args[0] === 'chat.history') {
        return Promise.resolve({
          messages: [
            {
              role: 'assistant',
              text: 'persisted answer',
              timestamp: `${today}T08:00:00.000Z`,
            },
          ],
          history_scope: 'complete',
        })
      }
      return Promise.resolve({})
    })
    renderPage()
    const thread = document.querySelector('.chat-thread') as HTMLElement

    await waitFor(() => {
      expect(thread.querySelector('.msg.assistant')).toHaveTextContent('persisted answer')
      expect(
        mockRpc.call.mock.calls.some((call) => call[0] === 'sessions.messages.subscribe'),
      ).toBe(true)
    })
    expect(thread.querySelectorAll('.chat-day-sep')).toHaveLength(1)

    const composer = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(composer, { target: { value: 'next turn' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => {
      expect(mockRpc.call.mock.calls.some((call) => call[0] === 'chat.send')).toBe(true)
      expect(thread.querySelector('.msg.user')).toHaveTextContent('next turn')
    })
    await act(async () => {
      mockRpc.emit('session.event.text_delta', { seq: 1, text: 'live answer' }, {})
    })

    expect(thread.querySelectorAll('.chat-day-sep')).toHaveLength(1)
    expect(thread.querySelector('.msg.assistant.streaming .msg-header')).not.toBeNull()
  })

  it('re-hydrates the mounted Visual-effects switch after a focus config refresh', async () => {
    mockRpc = makeRpc()
    mockRpc.call.mockImplementation((...args: unknown[]) => {
      if (args[0] === 'commands.list_for_surface') {
        return Promise.resolve({ surface: 'web_chat', commands: SLASH_CATALOG })
      }
      if (args[0] === 'config.get') {
        return Promise.resolve({ agentos_router: { enabled: true, tiers: {} } })
      }
      return Promise.resolve({})
    })
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: /run modes/i }))
    const toggle = await screen.findByRole('checkbox', { name: /visual effects/i })
    expect(toggle).toBeChecked()
    const initialConfigCalls = mockRpc.call.mock.calls.filter(
      (call) => call[0] === 'config.get',
    ).length

    localStorage.setItem('agentos-router-fx', JSON.stringify({ enabled: false }))
    await act(async () => {
      focusManager.setFocused(false)
      focusManager.setFocused(true)
    })

    await waitFor(() => {
      expect(
        mockRpc.call.mock.calls.filter((call) => call[0] === 'config.get').length,
      ).toBeGreaterThan(initialConfigCalls)
      expect(toggle).not.toBeChecked()
    })
  })

  it('sends the composed text via chat.send with the legacy payload (chat.js:6150/6193)', async () => {
    mockRpc = makeRpc()
    renderPage()
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'hello world' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => {
      const sends = mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send')
      expect(sends.length).toBe(1)
      const params = sends[0]![1] as Record<string, unknown>
      expect(params.message).toBe('hello world')
      expect(params.sessionKey).toBe('agent:main:webchat:default')
    })
  })

  it('enables an attachments-only send and threads attachments into chat.send (chat.js:6064/6157)', async () => {
    mockRpc = makeRpc()
    renderPage()
    // Attach an image via the composer file picker (fire change on the hidden input).
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    await act(async () => {
      fireEvent.change(fileInput, { target: { files: [makeImageFile('shot.png')] } })
    })
    // The inline FileReader resolves → the send button enables even with empty text.
    const send = await screen.findByRole('button', { name: /send/i })
    await waitFor(() => expect(send).toBeEnabled())
    fireEvent.click(send)
    await waitFor(() => {
      const sends = mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send')
      expect(sends.length).toBe(1)
      const params = sends[0]![1] as Record<string, unknown>
      // Empty-text attachments-only send → the fallback provider prompt.
      expect(params.message).toBe('Describe these attachments')
      const atts = params.attachments as Array<Record<string, unknown>>
      expect(atts).toHaveLength(1)
      expect(atts[0]?.name).toBe('shot.png')
      expect(atts[0]?.mime).toBe('image/png')
    })
  })

  it('opens the slash menu on "/" with the loaded catalog and executes a command (chat.js:2619/6113)', async () => {
    mockRpc = makeRpc()
    renderPage()
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    // Wait for the catalog to load, then type "/" → the menu opens.
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('commands.list_for_surface', {
        surface: 'web_chat',
      }),
    )
    fireEvent.change(ta, { target: { value: '/re' } })
    // The filtered command shows in the menu.
    expect(await screen.findByText('/reset')).toBeInTheDocument()
    // Enter (via the menu keyboard intercept) executes it → sessions.reset, NOT
    // a chat.send with the "/reset" text.
    fireEvent.keyDown(ta, { key: 'Enter' })
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('sessions.reset', {
        key: 'agent:main:webchat:default',
      }),
    )
    const sends = mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send')
    expect(sends.length).toBe(0)
  })

  it('a typed "/reset" sends as a slash command, not a chat message (chat.js:6113)', async () => {
    mockRpc = makeRpc()
    renderPage()
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('commands.list_for_surface', {
        surface: 'web_chat',
      }),
    )
    // A space closes the menu (args mode); click Send with the raw "/reset" text.
    fireEvent.change(ta, { target: { value: '/reset ' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('sessions.reset', {
        key: 'agent:main:webchat:default',
      }),
    )
    expect(mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send').length).toBe(0)
  })

  it('the "//" literal-slash escape sends "/help" as text, not a command (chat.js:6072)', async () => {
    mockRpc = makeRpc()
    renderPage()
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('commands.list_for_surface', {
        surface: 'web_chat',
      }),
    )
    // "//help" — the menu must NOT open (literal escape).
    fireEvent.change(ta, { target: { value: '//help' } })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => {
      const sends = mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send')
      expect(sends.length).toBe(1)
      // One "/" stripped → the literal message is "/help".
      expect((sends[0]![1] as Record<string, unknown>).message).toBe('/help')
    })
  })

  it('aborts the in-flight turn via chat.abort while streaming (chat.js:8444)', async () => {
    mockRpc = makeRpc()
    renderPage()
    // Drive the controller into a streaming state via a live text_delta frame.
    await act(async () => {
      mockRpc.emit('session.event.text_delta', { seq: 1, text: 'hi' }, {})
    })
    const abort = await screen.findByRole('button', { name: /abort|stop/i })
    fireEvent.click(abort)
    await waitFor(() => {
      const aborts = mockRpc.call.mock.calls.filter(([m]) => m === 'chat.abort')
      expect(aborts.length).toBe(1)
      const params = aborts[0]![1] as Record<string, unknown>
      expect(params.sessionKey).toBe('agent:main:webchat:default')
    })
    expect(toast.warning).toHaveBeenCalledWith('Stopped', { duration: 1800 })
  })

  it('drops a late text_delta after abort — the killed stream is NOT resurrected (chat.js:6652)', async () => {
    // Fix 1: `isAborted` must be wired into the controller. Without it,
    // `appendDelta`'s abort guard (stream.ts:839) is dead: a `text_delta`
    // buffered on the socket after the user hits Stop re-opens a stream bubble
    // and appends text to a turn the user explicitly killed.
    mockRpc = makeRpc()
    renderPage()
    const thread = document.querySelector('.chat-thread') as HTMLElement

    // Stream a delta → a streaming assistant bubble appears with committed text.
    await act(async () => {
      mockRpc.emit('session.event.text_delta', { seq: 1, text: 'partial ' }, {})
    })
    await waitFor(() => expect(thread.querySelector('.msg.assistant')).not.toBeNull())

    // User hits Stop → abort() sets abortedRef + ends streaming (aborted). The
    // bubble carried text, so it is finalized (streaming class dropped, an
    // `interrupted` mark added) — it is NOT still a live streaming bubble.
    const abort = await screen.findByRole('button', { name: /abort|stop/i })
    fireEvent.click(abort)
    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter(([m]) => m === 'chat.abort').length).toBe(1),
    )
    await waitFor(() => expect(thread.querySelector('.msg.assistant.streaming')).toBeNull())

    // Snapshot the finalized thread — a correctly-guarded late delta leaves it
    // byte-for-byte unchanged.
    const threadHtmlAfterAbort = thread.innerHTML
    const assistantCountAfterAbort = thread.querySelectorAll('.msg.assistant').length

    // A late `text_delta` still buffered on the socket now arrives. The abort
    // guard (stream.ts:839) must DROP it: no resurrected streaming bubble, no
    // second assistant bubble, no 'zombie text' appended to the killed turn.
    await act(async () => {
      mockRpc.emit('session.event.text_delta', { seq: 2, text: 'zombie text' }, {})
    })
    // Let any (buggy) rAF-batched render flush before asserting.
    await act(async () => {
      await Promise.resolve()
    })
    expect(thread.querySelector('.msg.assistant.streaming')).toBeNull()
    expect(thread.querySelectorAll('.msg.assistant').length).toBe(assistantCountAfterAbort)
    expect(thread.textContent).not.toContain('zombie text')
    expect(thread.innerHTML).toBe(threadHtmlAfterAbort)
  })

  it('pauses streaming auto-scroll after the reader moves away from the bottom (chat.js:2575)', async () => {
    mockRpc = makeRpc()
    renderPage()
    const thread = document.querySelector('.chat-thread') as HTMLElement
    await waitFor(() => expect(thread).toHaveAttribute('data-history-ready', 'true'))
    let scrollHeight = 1_000
    Object.defineProperties(thread, {
      clientHeight: { configurable: true, get: () => 300 },
      scrollHeight: { configurable: true, get: () => scrollHeight },
    })

    // The first delta starts the turn and follows its tail.
    await act(async () => {
      mockRpc.emit('session.event.text_delta', { seq: 1, text: 'first chunk' }, {})
    })
    expect(thread.scrollTop).toBe(1_000)

    // The reader scrolls well above the 60px near-bottom threshold.
    thread.scrollTop = 200
    fireEvent.scroll(thread)

    // More streamed content grows the document, but must not pull the reader
    // back down while they are reading the earlier section.
    scrollHeight = 1_200
    await act(async () => {
      mockRpc.emit('session.event.text_delta', { seq: 2, text: ' second chunk' }, {})
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
    })
    expect(thread.scrollTop).toBe(200)
  })

  it('renders a "Response timed out" row when the stream idle timer fires (stream.ts:522)', async () => {
    // Fix 2: `addMessage` must be wired into the controller. Without it, the
    // idle-timeout row (stream.ts:522) silently no-ops and a stalled stream ends
    // with no user-visible explanation. Drive the idle timer with fake timers +
    // a short grace negotiated via the `_hello` RPC policy.
    vi.useFakeTimers()
    try {
      mockRpc = makeRpc()
      renderPage()
      const thread = document.querySelector('.chat-thread') as HTMLElement

      // Negotiate a short idle grace so the backstop timer fires quickly
      // (applyRpcPolicy, stream.ts:547 → webui_stream_idle_grace_ms).
      act(() => {
        mockRpc.emit('_hello', { policy: { webui_stream_idle_grace_ms: 1000 } })
      })
      // Start a stream (arms the idle timer at the negotiated grace).
      act(() => {
        mockRpc.emit('session.event.text_delta', { seq: 1, text: 'working…' }, {})
      })
      expect(thread.querySelector('.msg.assistant')).not.toBeNull()

      // Advance past the idle grace → the timer finalizes the stream and appends
      // the 'Response timed out' error row via the now-wired `addMessage` dep.
      act(() => {
        vi.advanceTimersByTime(1100)
      })
      const errRow = thread.querySelector('.msg.error') as HTMLElement | null
      expect(errRow).not.toBeNull()
      expect(errRow!.textContent).toContain('Response timed out')
    } finally {
      vi.useRealTimers()
    }
  })

  it('opens the complete tool output from View full and closes the dialog (chat.js:7311)', async () => {
    mockRpc = makeRpc()
    renderPage()
    const thread = document.querySelector('.chat-thread') as HTMLElement
    await waitFor(() => expect(thread).toHaveAttribute('data-history-ready', 'true'))
    const fullOutput = `exit_code=0 ${'provider output '.repeat(20)}<not-html>`

    await act(async () => {
      mockRpc.emit(
        'session.event.tool_use_start',
        {
          key: 'agent:main:webchat:default',
          tool_use_id: 'tool-view-full',
          name: 'exec_command',
          input: { cmd: 'run provider check' },
        },
        {},
      )
      mockRpc.emit(
        'session.event.tool_result',
        {
          key: 'agent:main:webchat:default',
          tool_use_id: 'tool-view-full',
          name: 'exec_command',
          content: fullOutput,
        },
        {},
      )
    })

    const viewFull = await screen.findByRole('button', { name: 'View full' })
    fireEvent.click(viewFull)

    const dialog = screen.getByRole('dialog', { name: 'Tool Result' })
    expect(dialog).toHaveClass('chat-output-modal')
    expect(within(dialog).getByText('Tool output')).toBeInTheDocument()
    expect(within(dialog).getByText(`${fullOutput.length} characters`)).toBeInTheDocument()
    expect(dialog).toHaveTextContent(fullOutput)
    expect(dialog.querySelector('not-html')).toBeNull()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Tool Result' })).not.toBeInTheDocument(),
    )
  })

  it('updates the current-session run tag and clears it on a terminal frame', async () => {
    mockRpc = makeRpc()
    renderPage()
    const status = document.querySelector('#chat-run-status') as HTMLElement
    expect(status).toHaveTextContent('Idle')

    act(() => {
      mockRpc.emit('session.event.state_change', {
        key: 'agent:main:webchat:default',
        to_state: 'thinking',
        stream_seq: 1,
      })
    })
    await waitFor(() => expect(status).toHaveTextContent('Running'))

    act(() => {
      mockRpc.emit('task.running', {
        key: 'agent:main:webchat:default',
        task_id: 'task-1',
      })
    })
    expect(status).toHaveAttribute('data-status', 'running')

    act(() => {
      mockRpc.emit('sessions.changed', {
        key: 'agent:main:webchat:default',
        reason: 'turn_complete',
        run_status: 'idle',
      })
    })
    await waitFor(() => expect(status).toHaveTextContent('Idle'))
  })

  it('attaches real per-turn usage metadata to a completed assistant bubble', async () => {
    mockRpc = makeRpc()
    renderPage()
    act(() => {
      mockRpc.emit(
        'session.event.text_delta',
        { key: 'agent:main:webchat:default', stream_seq: 1, text: 'done' },
        {},
      )
      mockRpc.emit(
        'session.event.done',
        {
          key: 'agent:main:webchat:default',
          stream_seq: 2,
          text: 'done',
          model: 'openrouter/vendor/model-20260722',
          input_tokens: 1250,
          output_tokens: 42,
          cost_usd: 0.00125,
          routed_model: 'openrouter/vendor/model-20260722',
          routed_tier: 'c1',
          routing_source: 'pilot',
          total_savings_pct: 51,
        },
        {},
      )
    })

    const meta = await waitFor(() => {
      const node = document.querySelector('.msg.assistant .msg-meta') as HTMLElement | null
      expect(node).not.toBeNull()
      return node!
    })
    expect(meta).toHaveTextContent('model')
    expect(meta).toHaveTextContent('↑1.3k ↓42')
    expect(meta).toHaveTextContent('$0.00125')
    expect(meta.querySelector('.msg-meta__tokens')).toHaveAttribute(
      'title',
      'Turn — input: 1,250, output: 42 tokens',
    )
    expect(meta.querySelector('.msg-meta__saved')).toBeNull()
    expect(meta.querySelector('.msg-meta__combo')).toBeNull()
  })

  it('surfaces a subscription failure through the legacy error toast', async () => {
    mockRpc = makeRpc()
    mockRpc.call.mockImplementation((...args: unknown[]) => {
      if (args[0] === 'sessions.messages.subscribe') {
        return Promise.reject(new Error('socket unavailable'))
      }
      if (args[0] === 'commands.list_for_surface') {
        return Promise.resolve({ surface: 'web_chat', commands: SLASH_CATALOG })
      }
      return Promise.resolve({})
    })
    renderPage()

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'Session stream subscription failed: socket unavailable',
        { duration: 6000 },
      ),
    )
    await waitFor(() =>
      expect(document.querySelector('.chat-thread')).toHaveAttribute('data-history-ready', 'true'),
    )
  })

  it('shows a warning event as a transient warning toast, not a transcript row', async () => {
    mockRpc = makeRpc()
    renderPage()
    act(() => {
      mockRpc.emit('session.event.warning', {
        key: 'agent:main:webchat:default',
        message: 'Provider is warming up',
      })
    })

    expect(toast.warning).toHaveBeenCalledWith('Provider is warming up', { duration: 5000 })
    expect(document.querySelector('.msg.error')).toBeNull()
  })

  /* ── Session chip + lifecycle (Task 11) ─────────────────────────────────── */

  it('opens ?session=<key> and subscribes that session (chat.js:1211/2857)', async () => {
    mockRpc = makeRpc()
    renderPage('/chat?session=agent%3Atrader%3Awebchat%3Adefault')
    // The chip shows the URL session; the transcript subscribes it.
    expect(screen.getByText('agent:trader:webchat:default')).toBeInTheDocument()
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('sessions.messages.subscribe', {
        key: 'agent:trader:webchat:default',
        since_stream_seq: 0,
      }),
    )
  })

  it('opens ?agent=<id> as that agent’s webchat key (chat.js:1214)', async () => {
    mockRpc = makeRpc()
    renderPage('/chat?agent=trader')
    expect(screen.getByText('agent:trader:webchat:default')).toBeInTheDocument()
    // Persisted → the URL is rewritten to ?session= and ?agent= dropped (chat.js:1177).
    await waitFor(() => {
      expect(probe.search).toContain('session=agent%3Atrader%3Awebchat%3Adefault')
      expect(probe.search).not.toContain('agent=')
    })
  })

  it('switching sessions via the chip updates the URL ?session= (chat.js:1176/1809)', async () => {
    mockRpc = makeRpc()
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: /switch chat session/i }))
    fireEvent.click(await screen.findByText('agent:main:webchat:other'))
    await waitFor(() => expect(probe.search).toContain('session=agent%3Amain%3Awebchat%3Aother'))
    // The new session is subscribed (re-point → re-subscribe, chat.js:1832).
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('sessions.messages.subscribe', {
        key: 'agent:main:webchat:other',
        since_stream_seq: 0,
      }),
    )
  })

  it('persists the active session to localStorage (chat.js:1173)', async () => {
    mockRpc = makeRpc()
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: /switch chat session/i }))
    fireEvent.click(await screen.findByText('agent:main:webchat:other'))
    await waitFor(() =>
      expect(localStorage.getItem('agentos_active_session')).toBe('agent:main:webchat:other'),
    )
  })

  it('copies the session key from the chip (chat.js:1782)', async () => {
    mockRpc = makeRpc()
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    renderPage()
    await clickChatAction('Copy session key')
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith('agent:main:webchat:default')
      expect(toast.info).toHaveBeenCalledWith('Session key copied')
    })
  })

  it('resets the current session from the chip via sessions.reset (chat.js:2723)', async () => {
    mockRpc = makeRpc()
    renderPage()
    await clickChatAction('Reset session')
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('sessions.reset', {
        key: 'agent:main:webchat:default',
      }),
    )
  })

  it('closes Chat actions on Escape without aborting an active turn', async () => {
    mockRpc = makeRpc()
    renderPage()
    typeAndSend('keep streaming')
    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter(([method]) => method === 'chat.send')).toHaveLength(1),
    )

    const trigger = screen.getByRole('button', { name: 'Chat actions' })
    fireEvent.click(trigger)
    expect(await screen.findByRole('menu', { name: 'Chat actions' })).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByRole('menuitem', { name: 'Copy session key' })).toHaveFocus(),
    )

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('menu', { name: 'Chat actions' })).not.toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveFocus())
    expect(mockRpc.call.mock.calls.filter(([method]) => method === 'chat.abort')).toHaveLength(0)
  })

  it('closes Chat actions on Tab and preserves deterministic focus', async () => {
    mockRpc = makeRpc()
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }))
    const copy = await screen.findByRole('menuitem', { name: 'Copy session key' })
    await waitFor(() => expect(copy).toHaveFocus())

    fireEvent.keyDown(copy, { key: 'Tab' })

    expect(screen.queryByRole('menu', { name: 'Chat actions' })).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Chat actions' })).toHaveFocus())
  })

  it('starts a fresh session from the New chat action and returns focus to the composer', async () => {
    mockRpc = makeRpc()
    renderPage()
    const action = screen.getByRole('button', { name: 'New chat' })
    expect(action.querySelector('svg')).not.toBeNull()
    fireEvent.click(action)
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveFocus()
    await waitFor(() => {
      const subscriptions = mockRpc.call.mock.calls
        .filter(([method]) => method === 'sessions.messages.subscribe')
        .map(([, params]) => (params as { key: string }).key)
      expect(
        subscriptions.some(
          (key) => key.startsWith('agent:main:webchat:') && key !== 'agent:main:webchat:default',
        ),
      ).toBe(true)
    })
    expect(toast.info).toHaveBeenCalledWith(
      expect.stringContaining('New chat session in the current agent'),
    )
    expect(mockRpc.call.mock.calls.some(([method]) => method === 'chat.send')).toBe(false)
  })

  it('the "/new" slash command starts a new chat + subscribes it (chat.js:2692 via onSessionAction)', async () => {
    // Catalog carries a /new command whose action is new_chat.
    mockRpc = makeRpc()
    mockRpc.call = vi.fn((...args: unknown[]) => {
      if (args[0] === 'commands.list_for_surface') {
        return Promise.resolve({
          surface: 'web_chat',
          commands: [
            {
              name: '/new',
              usage: '/new',
              description: 'New chat',
              aliases: [],
              execution: { action: 'new_chat' },
            },
          ],
        })
      }
      return Promise.resolve({})
    }) as typeof mockRpc.call
    renderPage()
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('commands.list_for_surface', {
        surface: 'web_chat',
      }),
    )
    // Type "/new " (space closes the menu → args mode) and send it as a command.
    fireEvent.change(ta, { target: { value: '/new ' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    // onSessionAction('new_chat') → a fresh key in the SAME agent, switched to +
    // subscribed. The new key is a webchat key with a random suffix.
    await waitFor(() => {
      const subs = mockRpc.call.mock.calls
        .filter(([m]) => m === 'sessions.messages.subscribe')
        .map(([, p]) => (p as { key: string }).key)
      const newKey = subs.find(
        (k) => k.startsWith('agent:main:webchat:') && k !== 'agent:main:webchat:default',
      )
      expect(newKey).toBeTruthy()
    })
    // A new-chat toast fired, and no chat.send (the command was intercepted).
    expect(toast.info).toHaveBeenCalledWith(
      expect.stringContaining('New chat session in the current agent'),
    )
    expect(mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send').length).toBe(0)
  })

  it('parks the live stream on switch away and restores it on switch back (chat.js:1813/1831)', async () => {
    mockRpc = makeRpc()
    renderPage()
    // Drive a live stream on the default session → a stream bubble appears.
    await act(async () => {
      mockRpc.emit('session.event.text_delta', { seq: 1, text: 'streaming…' }, {})
    })
    const thread = document.querySelector('.chat-thread') as HTMLElement
    await waitFor(() => expect(thread.querySelector('.msg.assistant')).not.toBeNull())

    // Switch to another session — the outgoing session's live stream is parked
    // (its bubble removed from the DOM), and the new session subscribes.
    fireEvent.click(screen.getByRole('button', { name: /switch chat session/i }))
    fireEvent.click(await screen.findByText('agent:main:webchat:other'))
    await waitFor(() => expect(thread.querySelector('.msg.assistant')).toBeNull())

    // Switch back — the parked stream bubble is restored to the thread.
    fireEvent.click(screen.getByRole('button', { name: /switch chat session/i }))
    fireEvent.click(await screen.findByText('agent:main:webchat:default'))
    await waitFor(() => expect(thread.querySelector('.msg.assistant')).not.toBeNull())
  })

  // ── Pending queue (chat.js:6091-6110 enqueue-while-busy) ───────────────────

  const typeAndSend = (text: string) => {
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: text } })
    fireEvent.keyDown(ta, { key: 'Enter' })
  }

  it('enqueues a send while a turn is streaming — the pending rail renders (chat.js:6091)', async () => {
    mockRpc = makeRpc()
    renderPage()
    // First send starts streaming (the controller flips _isStreaming synchronously).
    typeAndSend('first message')
    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send').length).toBe(1),
    )
    // Second send while busy → enqueue, NOT a second chat.send.
    await act(async () => typeAndSend('queued while busy'))
    await waitFor(() => expect(screen.getByText('Pending 1/5')).toBeInTheDocument())
    expect(mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send').length).toBe(1)
    expect(screen.getByText('queued while busy')).toBeInTheDocument()
  })

  it('uses task.succeeded as a 75ms terminal backstop and drains pending', async () => {
    mockRpc = makeRpc()
    renderPage()
    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter(([method]) => method === 'chat.history').length).toBe(
        1,
      ),
    )
    typeAndSend('first message')
    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter(([method]) => method === 'chat.send').length).toBe(1),
    )
    await act(async () => typeAndSend('queued after terminal'))
    await waitFor(() => expect(screen.getByText('Pending 1/5')).toBeInTheDocument())
    const historyCallsBefore = mockRpc.call.mock.calls.filter(
      ([method]) => method === 'chat.history',
    ).length

    act(() => {
      mockRpc.emit('task.succeeded', {
        key: 'agent:main:webchat:default',
        task_id: 'task-success-backstop',
      })
    })

    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter(([method]) => method === 'chat.send').length).toBe(2),
    )
    expect(screen.queryByText('Pending 1/5')).not.toBeInTheDocument()
    await waitFor(() =>
      expect(
        mockRpc.call.mock.calls.filter(([method]) => method === 'chat.history').length,
      ).toBeGreaterThan(historyCallsBefore),
    )
  })

  it('caps the pending queue at MAX_PENDING (5) and toasts when full (chat.js:8511)', async () => {
    mockRpc = makeRpc()
    renderPage()
    typeAndSend('turn')
    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send').length).toBe(1),
    )
    for (let i = 0; i < 5; i++) {
      await act(async () => typeAndSend(`q${i}`))
    }
    await waitFor(() => expect(screen.getByText('Pending 5/5')).toBeInTheDocument())
    // A sixth enqueue is rejected with a "queue full" warning.
    await act(async () => typeAndSend('overflow'))
    expect(screen.getByText('Pending 5/5')).toBeInTheDocument()
    expect(toast.warning).toHaveBeenCalledWith(
      expect.stringContaining('Pending queue full (5)'),
      expect.anything(),
    )
  })

  it('recovers ALL pending into the composer on ESC (abort > recover, chat.js:2535/8596)', async () => {
    mockRpc = makeRpc()
    renderPage()
    typeAndSend('turn')
    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send').length).toBe(1),
    )
    await act(async () => typeAndSend('alpha'))
    await act(async () => typeAndSend('beta'))
    await waitFor(() => expect(screen.getByText('Pending 2/5')).toBeInTheDocument())

    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    // ESC while streaming: aborts (chat.abort) AND recovers pending into the input.
    await act(async () => {
      fireEvent.keyDown(ta, { key: 'Escape' })
    })
    await waitFor(() => {
      const aborts = mockRpc.call.mock.calls.filter(([m]) => m === 'chat.abort')
      expect(aborts.length).toBe(1)
    })
    // The queue is emptied and its texts joined into the composer (FIFO).
    await waitFor(() => expect(screen.queryByText('Pending 2/5')).not.toBeInTheDocument())
    expect(ta.value).toContain('alpha')
    expect(ta.value).toContain('beta')
  })

  it('removing a pending chip drops just that item (chat.js:8459)', async () => {
    mockRpc = makeRpc()
    renderPage()
    typeAndSend('turn')
    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send').length).toBe(1),
    )
    await act(async () => typeAndSend('keep'))
    await act(async () => typeAndSend('drop'))
    await waitFor(() => expect(screen.getByText('Pending 2/5')).toBeInTheDocument())
    const removeButtons = screen.getAllByRole('button', { name: /^Remove Pending message/ })
    fireEvent.click(removeButtons[1] as HTMLElement)
    await waitFor(() => expect(screen.getByText('Pending 1/5')).toBeInTheDocument())
    expect(screen.getByText('keep')).toBeInTheDocument()
    expect(screen.queryByText('drop')).not.toBeInTheDocument()
  })

  // ── Markdown export (chat.js:8389) ─────────────────────────────────────────

  it('exports the transcript as a Markdown download (chat.js:8389-8408)', async () => {
    mockRpc = makeRpc()
    renderPage()
    // Seed a rendered user message into the thread (the export source).
    typeAndSend('exported line')
    await waitFor(() => expect(document.querySelector('.msg.user')).not.toBeNull())

    // jsdom lacks URL.createObjectURL/revokeObjectURL — define them so the Blob
    // download path runs. Capture the anchor the export creates + clicks.
    const createObjectURL = vi.fn().mockReturnValue('blob:mock')
    const revokeObjectURL = vi.fn()
    ;(URL as unknown as { createObjectURL: unknown }).createObjectURL = createObjectURL
    ;(URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = revokeObjectURL
    const clickSpy = vi.fn()
    const origCreate = document.createElement.bind(document)
    const createSpy = vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = origCreate(tag) as HTMLElement
      if (tag === 'a') (el as HTMLAnchorElement).click = clickSpy
      return el
    })

    await clickChatAction('Export chat as Markdown')
    expect(clickSpy).toHaveBeenCalledTimes(1)
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(toast.info).toHaveBeenCalledWith('Exported as Markdown')
    createSpy.mockRestore()
  })

  // Run the export and return the Markdown string handed to the download Blob.
  // jsdom's Blob has no `.text()`, so capture the string ChatPage passes to
  // `new Blob([md])` (chat.js:8402) by intercepting the Blob constructor.
  async function runExportAndReadMarkdown(): Promise<string> {
    let captured = ''
    const RealBlob = globalThis.Blob
    const BlobSpy = vi.fn((parts?: unknown[]) => {
      if (Array.isArray(parts) && typeof parts[0] === 'string') captured = parts[0]
      return new RealBlob((parts as BlobPart[]) ?? [])
    })
    vi.stubGlobal('Blob', BlobSpy)
    ;(URL as unknown as { createObjectURL: unknown }).createObjectURL = vi
      .fn()
      .mockReturnValue('blob:mock')
    ;(URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn()
    const origCreate = document.createElement.bind(document)
    const createSpy = vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = origCreate(tag) as HTMLElement
      if (tag === 'a') (el as HTMLAnchorElement).click = vi.fn()
      return el
    })
    await clickChatAction('Export chat as Markdown')
    createSpy.mockRestore()
    vi.stubGlobal('Blob', RealBlob)
    return captured
  }

  it('export emits the `### role _(time)_` suffix for a row carrying data-history-ts (chat.js:8398)', async () => {
    mockRpc = makeRpc()
    renderPage()
    // The send-path user bubble stamps data-history-ts with the send time
    // (chat.js:6127-6130), so a plain send produces an export row with a ts.
    typeAndSend('exported line')
    await waitFor(() => expect(document.querySelector('.msg.user')).not.toBeNull())
    const row = document.querySelector('.msg.user') as HTMLElement
    // Pin a known timestamp so the asserted suffix is deterministic.
    const iso = '2026-07-21T09:00:00.000Z'
    row.dataset.historyTs = iso
    row.setAttribute('data-history-raw-text', 'exported line')

    const md = await runExportAndReadMarkdown()
    // chat.js:8398 — the suffix is ` _(new Date(ts).toLocaleString())_`.
    const expectedTime = new Date(iso).toLocaleString()
    expect(md).toContain(`_(${expectedTime})_`)
    // The header line is `### <role> _(<time>)_`.
    expect(md).toMatch(/### .+_\(.+\)_/)
  })

  it('stamps the meta caption (data-time HH:MM + data-sender) on the sent user bubble', async () => {
    mockRpc = makeRpc()
    renderPage()
    typeAndSend('meta caption line')
    await waitFor(() => expect(document.querySelector('.msg.user')).not.toBeNull())
    const row = document.querySelector('.msg.user') as HTMLElement
    // The CSS meta caption renders `attr(data-sender) attr(data-time)`; the
    // builder must stamp both so a user turn shows "YOU HH:MM" like the ref.
    expect(row.dataset.sender).toBe('YOU')
    expect(row.dataset.time).toMatch(/^\d{2}:\d{2}$/)
  })

  it('export omits the time suffix for a row WITHOUT data-history-ts (chat.js:8398 falsy branch)', async () => {
    mockRpc = makeRpc()
    renderPage()
    typeAndSend('no ts line')
    await waitFor(() => expect(document.querySelector('.msg.user')).not.toBeNull())
    const row = document.querySelector('.msg.user') as HTMLElement
    delete row.dataset.historyTs // simulate a message with no timestamp
    row.setAttribute('data-history-raw-text', 'no ts line')

    const md = await runExportAndReadMarkdown()
    expect(md).toContain('no ts line')
    expect(md).not.toMatch(/_\(.+\)_/)
  })

  it('export links an audio artifact via its child download anchor (chat.js:8411/8425)', async () => {
    mockRpc = makeRpc()
    renderPage()
    typeAndSend('audio turn')
    await waitFor(() => expect(document.querySelector('.msg.user')).not.toBeNull())
    // Inject an audio artifact card matching the renderer's shape (artifacts.ts:297):
    // data-artifact-name/-id on the card, data-artifact-download only on the child
    // Download anchor. The collector must still find the URL for audio.
    const row = document.querySelector('.msg.user') as HTMLElement
    const body = row.querySelector('.msg-body') as HTMLElement
    body.insertAdjacentHTML(
      'beforeend',
      `<div class="msg-artifact-card msg-artifact-card--audio" data-artifact-id="a1" data-artifact-name="clip.wav">
         <audio class="msg-artifact-audio" controls src="/download/clip.wav?sessionKey=k"></audio>
         <a class="msg-artifact-card__action" href="/download/clip.wav?sessionKey=k" download="clip.wav" data-artifact-download="/download/clip.wav">Download</a>
       </div>`,
    )

    const md = await runExportAndReadMarkdown()
    // chat.js:8420 — the artifact line is `- [Download <name>](<url>)`.
    expect(md).toContain('[Download clip.wav]')
    expect(md).toContain('/download/clip.wav')
  })

  it('toasts and skips export when the transcript is empty (chat.js:8390)', async () => {
    mockRpc = makeRpc()
    renderPage()
    await clickChatAction('Export chat as Markdown')
    expect(toast.warning).toHaveBeenCalledWith('No messages to export')
  })

  // ── Abort → done double-recover guard (chat.js:5122-5128) ──────────────────

  it('recovers pending ONCE across abort + the .done-wasAborted ack — no double drain (chat.js:5126-5128)', async () => {
    mockRpc = makeRpc()
    renderPage()
    // Start a streaming turn, then enqueue a message while it streams.
    typeAndSend('turn')
    await waitFor(() =>
      expect(mockRpc.call.mock.calls.filter(([m]) => m === 'chat.send').length).toBe(1),
    )
    await act(async () => typeAndSend('queued'))
    await waitFor(() => expect(screen.getByText('Pending 1/5')).toBeInTheDocument())

    // Spy on the recover model AFTER the queue is armed so we count only the
    // abort→done cycle's invocations. `usePendingQueue.popAllIntoComposer` calls
    // this model on every recover (usePendingQueue.ts:149), so a call here means
    // the recover path actually fired (not the queue-empty short-circuit).
    const recoverSpy = vi.spyOn(logicModule, 'popAllPendingIntoComposer')

    // User-initiated stop (ESC) → abortAndRecover: aborts AND recovers pending
    // into the composer (recover #1), and sets the stop-requested guard
    // (chat.js:8442).
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.keyDown(ta, { key: 'Escape' })
    })
    await waitFor(() => expect(screen.queryByText('Pending 1/5')).not.toBeInTheDocument())
    expect(ta.value).toContain('queued')
    expect(recoverSpy).toHaveBeenCalledTimes(1)

    // The server's `.done` (wasAborted) ack for the aborted turn arrives. The
    // guard (chat.js:5126-5128) must SKIP a second recover because the user-stop
    // path already ran — WITHOUT the guard this branch would call popAll again,
    // so a message enqueued in the abort→done window would be pulled in twice.
    await act(async () => {
      mockRpc.emit('chat.done', { sessionKey: 'agent:main:webchat:default', reason: 'aborted' })
    })
    // Still exactly one recover — the `.done`-abort drain did not re-run.
    expect(recoverSpy).toHaveBeenCalledTimes(1)
    recoverSpy.mockRestore()
  })
})
