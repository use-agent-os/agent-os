import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { useUi } from '~/stores/ui'
import { MemoryPane } from '../panes/MemoryPane'
import { useDraftStore } from './drafts'
import { openMemoryTab, useMemoryTab } from './tab-store'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, on: () => () => {}, waitForConnection: async () => {} }),
}))
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal: vi.fn() }, gateway: { restart: vi.fn() } }),
  isDesktop: () => true,
}))

interface Gateway {
  work: string[]
  you: string[]
  docs: { path: string; source: string; sizeBytes: number; lineCount: number }[]
  recall: Record<string, unknown>
  nudge: Record<string, unknown>
  embedding: Record<string, unknown>
}

let gw: Gateway

function snapshot() {
  return {
    revision: 'r1',
    config: {
      memory: {
        embedding: gw.embedding,
        recall: gw.recall,
        nudge: gw.nudge,
        curated_memory_char_limit: 4000,
        curated_user_char_limit: 2000,
        inject_limit: 6400,
      },
    },
    catalog: {
      memoryEmbeddingProviders: [
        'auto',
        'local',
        'ollama',
        'openai',
        'openai-compatible',
        'none',
      ].map((providerId) => ({
        providerId,
        runtimeSupported: true,
        requiresApiKey: providerId === 'openai' || providerId === 'openai-compatible',
        envKey: providerId === 'openai' ? 'OPENAI_API_KEY' : '',
      })),
    },
    status: {},
  }
}

function fakeGateway(method: string, params: Record<string, unknown> = {}): unknown {
  switch (method) {
    case 'config.snapshot':
      return snapshot()
    case 'memory.index':
      return { fileCount: 2, chunkCount: 5, vecAvailable: true, ftsAvailable: true }
    case 'memory.curated.get': {
      const entries = params.target === 'user' ? gw.you : gw.work
      return {
        agentId: 'main',
        target: params.target,
        entries,
        usage: '',
        charCount: entries.join('\n§\n').length,
        charLimit: params.target === 'user' ? 2000 : 4000,
        loadFailed: false,
      }
    }
    case 'memory.curated.add':
      ;(params.target === 'user' ? gw.you : gw.work).push(String(params.content))
      return {}
    case 'memory.curated.replace':
      gw.work = gw.work.map((e) => (e === params.oldText ? String(params.newContent) : e))
      return {}
    case 'memory.curated.remove':
      gw.work = gw.work.filter((e) => e !== params.oldText)
      gw.you = gw.you.filter((e) => e !== params.oldText)
      return {}
    case 'memory.knowledge_base.list':
      return { documents: gw.docs }
    case 'memory.knowledge_base.ingest':
      gw.docs.push({
        path: `knowledge_base/${String(params.filename)}`,
        source: 'knowledge_base',
        sizeBytes: String(params.content ?? '').length,
        lineCount: 1,
      })
      return { results: [{ status: 'indexed', chunksIndexed: 1 }] }
    case 'memory.list':
      return { files: [{ path: 'MEMORY.md', source: 'memory' }, ...gw.docs] }
    case 'memory.recall.status':
      return { config: { enabled: true }, recent: [] }
    case 'memory.search':
      return {
        results: [
          {
            chunkId: 'c1',
            path: 'knowledge_base/aurora.md',
            source: 'knowledge_base',
            startLine: 3,
            endLine: 5,
            snippet: 'The robots dock at charging bay 7.',
            score: 0.61,
          },
        ],
      }
    case 'config.patch':
      return { restartRequired: false }
    case 'onboarding.memory_embedding.configure':
      return { changed: true, restartRequired: true, warnings: [] }
    case 'sessions.list':
      return { sessions: [] }
    default:
      return {}
  }
}

function mount() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryPane />
    </QueryClientProvider>,
  )
}

function card(title: string): HTMLElement {
  return screen.getByRole('region', { name: title })
}

beforeEach(() => {
  gw = {
    work: ['The Q4 review is every second Thursday.'],
    you: [],
    docs: [],
    recall: { enabled: true, mode: 'escalate', max_injected_chars: 1800, deep_timeout_seconds: 8 },
    nudge: { enabled: true, interval: 10, before_compaction: true, timeout_seconds: 90 },
    embedding: { provider: 'auto' },
  }
  rpcCall.mockReset()
  rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) =>
    fakeGateway(method, params),
  )
  vi.mocked(toast.success).mockClear()
  vi.mocked(toast.error).mockClear()
  useConnection.getState().setState('connected')
  useMemoryTab.setState({ tab: 'remembered' })
  useDraftStore.setState({ drafts: {} })
  // Ollama is not running on the test machine.
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('MemoryPane · summary', () => {
  it('opens on what the agent remembers, with the search state as the head pill', async () => {
    mount()
    expect(await screen.findByText('Finds by meaning')).toBeInTheDocument()
    const stats = screen.getByRole('group', { name: 'Memory at a glance' })
    await waitFor(() =>
      expect(within(stats).getByText('1 on work · 0 about you')).toBeInTheDocument(),
    )
    expect(within(stats).getByText('By meaning')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Remembered/ })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByText('The Q4 review is every second Thursday.')).toBeInTheDocument()
    // An empty list explains itself and offers the way to add.
    expect(within(card('About you')).getByText('Nothing remembered yet')).toBeInTheDocument()
  })

  it('offline: says so, never spins, and calls nothing', () => {
    useConnection.getState().setState('disconnected')
    mount()
    expect(screen.getByText('Offline')).toBeInTheDocument()
    expect(screen.getByText('Start the gateway to configure this.')).toBeInTheDocument()
    expect(screen.queryByText('Loading')).not.toBeInTheDocument()
    expect(rpcCall).not.toHaveBeenCalled()
  })

  it('opens on a given tab through openMemoryTab', () => {
    act(() => openMemoryTab('setup'))
    expect(useUi.getState().settingsOpen).toBe(true)
    expect(useUi.getState().settingsSection).toBe('memory')
    mount()
    expect(screen.getByRole('tab', { name: /Setup/ })).toHaveAttribute('aria-selected', 'true')
  })
})

describe('MemoryPane · Remembered', () => {
  it('adds a fact in place, without a browser prompt', async () => {
    mount()
    const you = card('About you')
    fireEvent.click(await within(you).findByRole('button', { name: /Add a fact/ }))
    const box = within(you).getByRole('textbox', { name: 'Fact' })
    fireEvent.change(box, { target: { value: 'Prefers short answers.' } })
    fireEvent.keyDown(box, { key: 'Enter', metaKey: true })
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('memory.curated.add', {
        agentId: 'main',
        target: 'user',
        content: 'Prefers short answers.',
      }),
    )
    expect(await within(you).findByText('Prefers short answers.')).toBeInTheDocument()
  })

  it('edits a fact with replace, keyed by its old text', async () => {
    mount()
    const work = card('About your work')
    fireEvent.click(await within(work).findByRole('button', { name: /^Edit “The Q4/ }))
    fireEvent.change(within(work).getByRole('textbox', { name: 'Fact' }), {
      target: { value: 'The Q4 review is every second Thursday at 10:00.' },
    })
    fireEvent.click(within(work).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('memory.curated.replace', {
        agentId: 'main',
        target: 'memory',
        oldText: 'The Q4 review is every second Thursday.',
        newContent: 'The Q4 review is every second Thursday at 10:00.',
      }),
    )
  })

  it('asks before removing a fact', async () => {
    mount()
    const work = card('About your work')
    fireEvent.click(await within(work).findByRole('button', { name: /^Remove “The Q4/ }))
    expect(rpcCall).not.toHaveBeenCalledWith('memory.curated.remove', expect.anything())
    const dialog = screen.getByRole('alertdialog', { name: 'Remove this fact?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('memory.curated.remove', {
        agentId: 'main',
        target: 'memory',
        oldText: 'The Q4 review is every second Thursday.',
      }),
    )
  })

  it('will not save a fact that overflows its list', async () => {
    gw.you = ['x'.repeat(1990)]
    mount()
    const you = card('About you')
    fireEvent.click(await within(you).findByRole('button', { name: /Add a fact to About you/ }))
    fireEvent.change(within(you).getByRole('textbox', { name: 'Fact' }), {
      target: { value: 'twelve chars' },
    })
    expect(within(you).getByText(/characters over the limit/)).toBeInTheDocument()
    expect(within(you).getByRole('button', { name: 'Save' })).toBeDisabled()
    // Nearly full lists say what happens when they fill up.
    expect(within(you).getByText(/Almost full/)).toBeInTheDocument()
  })
})

describe('MemoryPane · Knowledge and Try it', () => {
  it('reads a chosen file and sends its text; refuses a PDF with a way forward', async () => {
    useMemoryTab.setState({ tab: 'knowledge' })
    mount()
    const docs = card('Documents')
    const input = (await within(docs).findByLabelText('Choose files', {
      selector: 'input',
    })) as HTMLInputElement
    const md = new File(['Robots dock at bay 7.'], 'aurora.md', { type: 'text/markdown' })
    const pdf = new File(['%PDF'], 'contract.pdf', { type: 'application/pdf' })
    fireEvent.change(input, { target: { files: [md, pdf] } })
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('memory.knowledge_base.ingest', {
        agentId: 'main',
        filename: 'aurora.md',
        content: 'Robots dock at bay 7.',
      }),
    )
    expect(rpcCall).not.toHaveBeenCalledWith(
      'memory.knowledge_base.ingest',
      expect.objectContaining({ filename: 'contract.pdf' }),
    )
    expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('contract.pdf: PDF'))
    expect(await within(docs).findByText('aurora.md')).toBeInTheDocument()
  })

  it('searches as you type and shows where each match came from', async () => {
    useMemoryTab.setState({ tab: 'try' })
    mount()
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search memory' }), {
      target: { value: 'where do robots charge' },
    })
    expect(await screen.findByText('aurora.md', {}, { timeout: 2000 })).toBeInTheDocument()
    expect(rpcCall).toHaveBeenCalledWith('memory.search', {
      agentId: 'main',
      query: 'where do robots charge',
      source: 'all',
      minScore: 0.35,
      limit: 10,
    })
    expect(screen.getByText('lines 3–5')).toBeInTheDocument()
    expect(screen.getByText('61%')).toBeInTheDocument()
  })
})

describe('MemoryPane · Setup', () => {
  it('writes recall through config.patch with the revision, and says it applies live', async () => {
    useMemoryTab.setState({ tab: 'setup' })
    mount()
    const recall = await screen.findByRole('region', { name: 'Recall before each reply' })
    fireEvent.click(within(recall).getByRole('switch', { name: 'Look things up before replying' }))
    // An unsaved edit is marked on the tab.
    expect(screen.getByRole('img', { name: 'Unsaved changes' })).toBeInTheDocument()
    fireEvent.click(within(recall).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('config.patch', {
        patches: { 'memory.recall.enabled': false },
        expectedRevision: 'r1',
      }),
    )
    expect(toast.success).toHaveBeenCalledWith('Applied. No restart needed.', expect.anything())
  })

  it('writes the review interval as a number', async () => {
    useMemoryTab.setState({ tab: 'setup' })
    mount()
    const review = await screen.findByRole('region', { name: 'Learn from conversations' })
    fireEvent.click(within(review).getByRole('radio', { name: '20' }))
    fireEvent.click(within(review).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('config.patch', {
        patches: { 'memory.nudge.interval': 20 },
        expectedRevision: 'r1',
      }),
    )
  })

  it('switches to Ollama through the guided write, says Ollama is not running, and asks for a restart', async () => {
    useMemoryTab.setState({ tab: 'setup' })
    mount()
    const embed = await screen.findByRole('region', { name: 'How memory is searched' })
    fireEvent.click(within(embed).getByRole('radio', { name: /Ollama/ }))
    expect(
      await within(embed).findByText(/Ollama is not running at http:\/\/localhost:11434/),
    ).toBeInTheDocument()
    expect(within(embed).getByRole('textbox', { name: 'Model' })).toHaveAttribute(
      'placeholder',
      'embeddinggemma',
    )
    fireEvent.click(within(embed).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('onboarding.memory_embedding.configure', {
        providerId: 'ollama',
        model: '',
        baseUrl: '',
        expectedRevision: 'r1',
      }),
    )
    expect(toast.success).toHaveBeenCalledWith(
      'Saved. Restart the gateway to switch.',
      expect.anything(),
    )
  })

  it('needs a key before saving OpenAI', async () => {
    useMemoryTab.setState({ tab: 'setup' })
    gw.embedding = { provider: 'auto', remote: { api_key_env: '' } }
    mount()
    const embed = await screen.findByRole('region', { name: 'How memory is searched' })
    fireEvent.click(within(embed).getByRole('radio', { name: /^OpenAI/ }))
    // The catalog's env name is offered, so the key can come from the gateway's environment.
    fireEvent.click(within(embed).getByRole('button', { name: /Advanced/ }))
    const env = within(embed).getByRole('textbox', { name: 'Key from an environment variable' })
    expect(env).toHaveValue('OPENAI_API_KEY')
    fireEvent.change(env, { target: { value: '' } })
    expect(within(embed).getByText(/Paste an API key/)).toBeInTheDocument()
    expect(within(embed).getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('reloads and keeps the edit when someone else wrote the config first', async () => {
    useMemoryTab.setState({ tab: 'setup' })
    mount()
    const review = await screen.findByRole('region', { name: 'Learn from conversations' })
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'config.patch') throw new Error('config revision mismatch: expected r1')
      return fakeGateway(method, params)
    })
    fireEvent.click(within(review).getByRole('radio', { name: '5' }))
    fireEvent.click(within(review).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringContaining('changed elsewhere'),
        expect.anything(),
      ),
    )
    expect(within(review).getByRole('radio', { name: '5' })).toHaveAttribute('aria-checked', 'true')
  })

  it('previews a forget before removing anything', async () => {
    useMemoryTab.setState({ tab: 'setup' })
    rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'memory.forget') {
        return {
          dryRun: params?.dryRun,
          sessions: [{ selector: 'agent:main:desk:1' }],
          curated: [{ target: 'memory', entry: 'Likes tea.', mixed: false }],
          notes: [],
          sessionDocuments: ['s1'],
        }
      }
      return fakeGateway(method, params)
    })
    mount()
    const maint = await screen.findByRole('region', { name: 'Maintenance' })
    fireEvent.click(within(maint).getByRole('button', { name: /Choose/ }))
    const sheet = await screen.findByRole('dialog', { name: 'Forget a conversation' })
    fireEvent.change(within(sheet).getByRole('combobox'), { target: { value: '__manual__' } })
    fireEvent.change(within(sheet).getByRole('textbox', { name: 'Session id or key' }), {
      target: { value: 'agent:main:desk:1' },
    })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Show what would go' }))
    expect(await within(sheet).findByText('Likes tea.')).toBeInTheDocument()
    expect(rpcCall).toHaveBeenCalledWith('memory.forget', {
      agentId: 'main',
      sessions: ['agent:main:desk:1'],
      dryRun: true,
    })
    expect(rpcCall).not.toHaveBeenCalledWith(
      'memory.forget',
      expect.objectContaining({ dryRun: false }),
    )
    fireEvent.click(within(sheet).getByRole('button', { name: 'Forget' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('memory.forget', {
        agentId: 'main',
        sessions: ['agent:main:desk:1'],
        dryRun: false,
      }),
    )
  })
})
