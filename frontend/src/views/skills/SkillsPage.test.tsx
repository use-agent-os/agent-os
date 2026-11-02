import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { SkillsPage } from './SkillsPage'
import bankrSymbolUrl from '@/assets/bankr-symbol.svg'
import capminalSymbolUrl from '@/assets/capminal-symbol.svg'
import robinhoodSymbolUrl from '@/assets/robinhood-symbol.png'
import museSymbolUrl from '@/assets/muse-symbol.png'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}))

const navigateSpy = vi.fn()
vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router')
  return { ...actual, useNavigate: () => navigateSpy }
})

type Handler = (...args: unknown[]) => void
function makeRpc() {
  const listeners = new Map<string, Set<Handler>>()
  return {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn(),
    on: vi.fn((event: string, handler: Handler) => {
      if (!listeners.has(event)) listeners.set(event, new Set())
      listeners.get(event)!.add(handler)
      return () => listeners.get(event)?.delete(handler)
    }),
    emit(event: string, ...args: unknown[]) {
      listeners.get(event)?.forEach((h) => h(...args))
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
    features: {},
  }),
}))

const READY_MANAGED = {
  name: 'trader',
  description: 'Trades things',
  layer: 'managed',
  acquisition: {
    kind: 'hub',
    source_id: 'clawhub',
    identifier: 'uniswap-swap',
    removable: true,
    updatable: true,
  },
  publisher: { id: '', name: '', url: '', logo: '' },
  availability: { offered: true, reason: '', detail: '' },
  status: 'ready',
  emoji: '📈',
}
const NEEDS_BUNDLED = {
  name: 'weather',
  description: 'Weather lookups',
  layer: 'bundled',
  acquisition: { kind: 'shipped', removable: false, updatable: false },
  publisher: { id: '', name: '', url: '', logo: '' },
  availability: {
    offered: false,
    reason: 'ineligible',
    detail: 'Installed, but not offered to the agent: the binary curl is not on PATH.',
  },
  status: 'needs_setup',
  missing_bins: ['curl'],
  install: [{ id: 'brew-curl', kind: 'brew', label: 'Install curl', bins: ['curl'] }],
}
// A hub install the gateway will not offer to remove: `skills.uninstall` only
// deletes from the configured managed dir, and this one is recorded elsewhere.
const STRANDED_HUB = {
  name: 'stranded',
  description: 'Installed from a hub AgentOS can no longer delete from.',
  layer: 'managed',
  acquisition: {
    kind: 'hub',
    source_id: 'clawhub',
    identifier: 'stranded-skill',
    removable: false,
    updatable: true,
  },
  status: 'ready',
}
// Hand-copied into the managed directory: same `layer`, no lockfile entry, so
// neither affordance applies. Grouping and buttons must follow acquisition.
const LOCAL_IN_MANAGED = {
  name: 'handrolled',
  description: 'Copied in by hand',
  layer: 'managed',
  acquisition: { kind: 'local', removable: false, updatable: false },
  status: 'ready',
}
const BANKR_HUB_SKILL = {
  name: 'bankr-swaps',
  description: 'Swap tokens through Bankr.',
  layer: 'managed',
  acquisition: { kind: 'hub', source_id: 'bankr', identifier: 'bankr-swaps', removable: true },
  publisher: { id: 'bankr', name: 'Bankr', url: 'https://bankr.bot', logo: '' },
  status: 'ready',
}
// A hub install whose catalog row named a publisher the allowlist does not
// know: it resolves to no brand and must not wear one, so the hub id and the
// author handle are all the provenance the card has left.
const COMMUNITY_HUB_SKILL = {
  name: 'stranger-skill',
  description: 'Published by someone the allowlist has never heard of.',
  layer: 'managed',
  acquisition: {
    kind: 'hub',
    source_id: 'clawhub',
    author: '@somebody',
    identifier: 'https://clawhub.example/skills/stranger-skill',
    removable: true,
    updatable: true,
  },
  publisher: { id: '', name: '', url: '', logo: '' },
  status: 'ready',
}
// Published from a wallet on Bankr's hub, but named in the wheel's user-skill
// allowlist — so it is Bankr-distributed and groups with Bankr, while the
// wallet handle stays a credit rather than a second brand.
const WALLET_PARTNER_SKILL = {
  name: 'stock-premium-lp-manager',
  description: 'Manage range liquidity in tokenized-equity pools.',
  layer: 'managed',
  acquisition: {
    kind: 'hub',
    source_id: 'bankr',
    author: '@igoryuzo',
    identifier: 'https://bankr.bot/skills/0xdead/stock-premium-lp-manager',
    removable: true,
    updatable: true,
  },
  publisher: { id: 'bankr', name: 'Bankr', url: 'https://bankr.bot', logo: '' },
  status: 'ready',
}
// An AgentOS-authored crypto skill: no publisher, so no partner brand, but it
// lands in the "AgentOS Crypto Skills" group and wears the AgentOS mark. The
// group is gated on shipped/bundled, so a local drop-in cannot claim the mark.
const AGENTOS_CRYPTO_SKILL = {
  name: 'senior-unilp-manager',
  description: 'Query and manage Uniswap V4 liquidity.',
  layer: 'bundled',
  acquisition: { kind: 'shipped', removable: false, updatable: false },
  publisher: { id: '', name: '', url: '', logo: '' },
  category: 'crypto',
  status: 'ready',
}
// One of the bundled GMGN skills: the same crypto group as the one above, but it
// wears the GMGN mark plus its own emoji so seven sibling cards stay tellable
// apart. The brand comes off `provenance`, never off the `gmgn-` name.
const GMGN_SKILL = {
  name: 'gmgn-token',
  description: 'Research any crypto or meme token by address.',
  layer: 'bundled',
  acquisition: { kind: 'shipped', removable: false, updatable: false },
  publisher: { id: '', name: '', url: '', logo: '' },
  provenance: { origin: 'gmgn-mit', license: 'MIT', maintained_by: 'AgentOS' },
  category: 'crypto',
  emoji: '🔎',
  status: 'ready',
}
// Ready, eligible, and still never reaching the model.
const WITHHELD_SKILL = {
  name: 'quiet',
  description: 'The agent is never told about this one.',
  layer: 'bundled',
  acquisition: { kind: 'shipped' },
  status: 'ready',
  eligible: true,
  availability: {
    offered: false,
    reason: 'model_invocation_disabled',
    detail: 'This skill declares disable_model_invocation, so the agent is never offered it.',
  },
}
const CATALOG_ITEM = {
  name: 'Uniswap',
  identifier: 'uniswap-swap',
  provider: 'Uniswap',
  source: 'clawhub',
  logo: 'https://raw.githubusercontent.com/BankrBot/skills/main/uniswap/uniswap.svg',
  description: 'DEX swaps',
  category: 'defi',
  demo: { code: 'swap(1, ETH)', language: 'python', title: 'Quote a swap' },
}
const BANKR_CATALOG_ITEM = {
  name: 'bankr-token-scam-analysis',
  identifier: 'bankr-token-scam-analysis',
  provider: 'BankrBot',
  source: 'bankr',
  description: 'Analyze token risk.',
}
const CAPMINAL_CATALOG_ITEM = {
  name: 'morse-launch-b20',
  identifier: 'https://github.com/Capminal/agent-skills/tree/main/morse-launch-b20',
  provider: 'Capminal',
  source: 'capminal',
  description: 'Morse launch B20 skill.',
}
// A needs_setup skill carrying both a Requirements manifest and Missing bins/env
// (skills.js:743-777,792-803). requirements.items exercises the ready /
// needs_setup / missing_skill status branches plus the missing + requires detail.
const REQ_BUNDLED = {
  name: 'oracle',
  description: 'On-chain oracle reads',
  layer: 'bundled',
  status: 'needs_setup',
  missing_bins: ['jq'],
  missing_env: ['ORACLE_KEY'],
  requirements: {
    items: [
      {
        name: 'jq',
        status: 'needs_setup',
        missing_bins: ['jq'],
      },
      {
        name: 'apikey',
        status: 'missing_skill',
        requires_env: ['ORACLE_KEY'],
      },
      {
        name: 'python',
        status: 'ready',
        requires_bins: ['python3'],
      },
    ],
  },
}

// Partner identity comes from the server-side publisher allowlist, never from
// the `robinhood-` name prefix — the client no longer reads the name at all.
const ROBINHOOD_READY = {
  name: 'robinhood-rwa-addresses',
  description: 'Look up tokenized-stock contract addresses.',
  layer: 'bundled',
  acquisition: { kind: 'shipped' },
  publisher: { id: 'robinhood', name: 'Robinhood', url: 'https://robinhood.com', logo: '' },
  status: 'ready',
}

const ROBINHOOD_UNDECLARED = {
  name: 'robinhood-agentic-trading',
  description: 'Operate Robinhood Agentic Trading through the Robinhood Trading MCP.',
  layer: 'bundled',
  acquisition: { kind: 'shipped' },
  publisher: { id: 'robinhood', name: 'Robinhood', url: 'https://robinhood.com', logo: '' },
  status: 'not_declared',
}

// The status filters need a skill that genuinely is not ready. `not_declared`
// no longer qualifies: a skill that declared no requirements runs, so the UI
// counts it as Ready.
const ROBINHOOD_NEEDS = {
  name: 'robinhood-options',
  description: 'Options chains, if the CLI is installed.',
  layer: 'bundled',
  acquisition: { kind: 'shipped' },
  publisher: { id: 'robinhood', name: 'Robinhood', url: 'https://robinhood.com', logo: '' },
  status: 'needs_setup',
  missing_bins: ['rh'],
}

// The second bundled partner. Nothing about the name says "musebook" to the
// client; the tab is populated from `publisher.id` alone.
const MUSEBOOK_READY = {
  name: 'musebook',
  description: 'Join and take part in the muse boards.',
  layer: 'bundled',
  acquisition: { kind: 'shipped' },
  publisher: { id: 'muse', name: 'Muse', url: 'https://musebook.lol', logo: '' },
  status: 'ready',
}

// A catalog row the empty-query browse does NOT return — the case where the
// installed row used to vanish the moment the search was cleared.
const SEARCH_ONLY_ITEM = {
  name: 'Ledger Watch',
  identifier: 'ledger-watch',
  provider: 'Ledger',
  source: 'clawhub',
  description: 'Watch a ledger address.',
}

function wireRpc(
  opts: {
    skills?: unknown[]
    skillsSequence?: unknown[][]
    listPromise?: Promise<unknown>
    listReject?: boolean
    searchResults?: unknown[]
    /**
     * The catalog's own answer to a query. The mock used to return the same
     * rows for every call, which is exactly why a list that swaps on the query
     * text could break without a test noticing.
     */
    search?: (query: string, source: string) => unknown[] | Promise<{ results: unknown[] }>
    installResponse?: Record<string, unknown>
    uninstallResponse?: Record<string, unknown>
    updateResponse?: Record<string, unknown>
    depsResponse?: Record<string, unknown>
  } = {},
) {
  let listIndex = 0
  mockRpc.call.mockImplementation((method: string, params?: unknown) => {
    const p = (params ?? {}) as { query?: string; source?: string }
    switch (method) {
      case 'skills.list':
        if (opts.listPromise) return opts.listPromise
        if (opts.skillsSequence) {
          const index = Math.min(listIndex, opts.skillsSequence.length - 1)
          listIndex += 1
          return Promise.resolve({ skills: opts.skillsSequence[index] ?? [] })
        }
        return opts.listReject
          ? Promise.reject(new Error('list down'))
          : Promise.resolve({ skills: opts.skills ?? [READY_MANAGED, NEEDS_BUNDLED] })
      case 'skills.search': {
        const query = (p.query ?? '').trim()
        const source = p.source ?? ''
        if (opts.search) {
          const answer = opts.search(query, source)
          return Array.isArray(answer) ? Promise.resolve({ results: answer }) : answer
        }
        const all = (opts.searchResults ?? [CATALOG_ITEM]) as Record<string, unknown>[]
        const scoped = source ? all.filter((r) => r.source === source) : all
        const q = query.toLowerCase()
        const results = q
          ? scoped.filter((r) =>
              [r.name, r.description, r.provider].some((f) =>
                String(f ?? '')
                  .toLowerCase()
                  .includes(q),
              ),
            )
          : scoped
        return Promise.resolve({ results })
      }
      case 'skills.install':
        return Promise.resolve(opts.installResponse ?? { success: true, name: 'uniswap-swap' })
      case 'skills.uninstall':
        return Promise.resolve(opts.uninstallResponse ?? { success: true })
      case 'skills.update':
        return Promise.resolve(opts.updateResponse ?? { results: [{ success: true }] })
      case 'skills.deps.install':
        return Promise.resolve(opts.depsResponse ?? { success: true, missing_still: {} })
      default:
        return Promise.resolve({})
    }
  })
}

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function renderPage(client: QueryClient = makeClient()) {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <SkillsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** The <details> block whose heading is `label`, i.e. one Installed group. */
function groupNamed(label: string): HTMLElement {
  const heading = screen.getByRole('heading', { name: label })
  const block = heading.closest('details')
  expect(block).not.toBeNull()
  return block as HTMLElement
}

const callsFor = (m: string) => mockRpc.call.mock.calls.filter(([x]) => x === m)

describe('SkillsPage', () => {
  beforeEach(() => {
    mockRpc = makeRpc()
    navigateSpy.mockClear()
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.error).mockClear()
    vi.mocked(toast.info).mockClear()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('calls skills.list after waitForConnection and groups installed cards by provenance', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('skills.list', {}))
    expect(mockRpc.waitForConnection).toHaveBeenCalled()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    expect(screen.getByLabelText('Skill weather')).toBeInTheDocument()
    // Provenance groups: shipped before hub (SKILL_GROUP_ORDER), not layers.
    expect(screen.getByText('AgentOS Normal Skills')).toBeInTheDocument()
    expect(screen.getByText('Installed from a hub')).toBeInTheDocument()
  })

  it('renders the status metric pills from the payload', async () => {
    wireRpc()
    renderPage()
    // 1 ready, 1 needs_setup, total 2
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Filter: All' })).toHaveTextContent('2'),
    )
    expect(screen.getByRole('button', { name: 'Filter: Ready' })).toHaveTextContent('1')
    expect(screen.getByRole('button', { name: 'Filter: Needs setup' })).toHaveTextContent('1')
  })

  it('renders an accessible source navigator with the official partner logos', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())

    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((tab) => tab.getAttribute('aria-label'))).toEqual([
      'Installed',
      'Robinhood',
      'Muse',
      'Bankr',
      'Aeon',
      'Capminal',
      'Community',
    ])
    expect(
      within(screen.getByRole('tab', { name: 'Bankr' }))
        .getByRole('presentation')
        .getAttribute('src'),
    ).toBe(bankrSymbolUrl)
    expect(
      within(screen.getByRole('tab', { name: 'Capminal' }))
        .getByRole('presentation')
        .getAttribute('src'),
    ).toBe(capminalSymbolUrl)
    expect(
      within(screen.getByRole('tab', { name: 'Robinhood' }))
        .getByRole('presentation')
        .getAttribute('src'),
    ).toBe(robinhoodSymbolUrl)
  })

  it('supports arrow-key navigation across skill sources', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())

    // Walks whatever order the tabs render in rather than naming neighbours,
    // so reordering the partner tabs does not falsely fail this test — the
    // behaviour under test is that ArrowRight advances one tab, not which
    // brand happens to sit second.
    const order = screen.getAllByRole('tab').map((el) => el.getAttribute('aria-label'))
    expect(order.length).toBeGreaterThan(2)

    let current = screen.getByRole('tab', { name: order[0]! })
    current.focus()

    for (const next of order.slice(1)) {
      fireEvent.keyDown(current, { key: 'ArrowRight' })
      const el = screen.getByRole('tab', { name: next! })
      expect(el).toHaveAttribute('aria-selected', 'true')
      expect(el).toHaveFocus()
      current = el
    }

    // And wraps back to the first tab from the last.
    fireEvent.keyDown(current, { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: order[0]! })).toHaveFocus()
  })

  it('the status pill filters the installed list', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill weather')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Filter: Ready' }))
    await waitFor(() => expect(screen.queryByLabelText('Skill weather')).not.toBeInTheDocument())
    expect(screen.getByLabelText('Skill trader')).toBeInTheDocument()
  })

  it('the header filter searches name/description', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Filter installed skills'), {
      target: { value: 'weather' },
    })
    await waitFor(() => expect(screen.queryByLabelText('Skill trader')).not.toBeInTheDocument())
    expect(screen.getByLabelText('Skill weather')).toBeInTheDocument()
  })

  it('toasts when skills.list fails', async () => {
    wireRpc({ listReject: true })
    renderPage()
    await waitFor(() => expect(toast.error).toHaveBeenCalled())
  })

  // ── Registry search debounce (skills.js:210-220, 250ms) ──────────────────
  // Only the FINAL keystroke reaches the server, and it does so exactly once:
  // rapid typing coalesces to a single skills.search for the settled value.
  it('community search fires skills.search once after the debounce interval', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    // snapshot fetch (query:'') fires on tab entry
    const input = await screen.findByLabelText('Search community skills')
    const searchesFor = (q: string) =>
      callsFor('skills.search').filter(([, p]) => (p as { query?: string })?.query === q)
    // Type three quick keystrokes.
    fireEvent.change(input, { target: { value: 'u' } })
    fireEvent.change(input, { target: { value: 'un' } })
    fireEvent.change(input, { target: { value: 'uni' } })
    // The debounced search for the settled value fires exactly once.
    await waitFor(() => expect(searchesFor('uni')).toHaveLength(1))
    // The intermediate keystrokes never reached the server.
    expect(searchesFor('u')).toHaveLength(0)
    expect(searchesFor('un')).toHaveLength(0)
  })

  it('loads registry logos without sending the operator page as a referrer', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))

    const card = await screen.findByLabelText('Catalog skill Uniswap')
    const logo = within(card).getByRole('presentation')

    expect(logo).toHaveAttribute('src', CATALOG_ITEM.logo)
    expect(logo).toHaveAttribute('referrerpolicy', 'no-referrer')
  })

  it('uses the official Bankr icon when a Bankr catalog item has no logo metadata', async () => {
    wireRpc({ searchResults: [BANKR_CATALOG_ITEM] })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: 'Bankr' }))

    const card = await screen.findByLabelText('Catalog skill bankr-token-scam-analysis')
    expect(within(card).getByRole('presentation')).toHaveAttribute('src', bankrSymbolUrl)
  })

  it('uses the official Capminal icon when a Capminal catalog item has no logo metadata', async () => {
    wireRpc({ searchResults: [CAPMINAL_CATALOG_ITEM] })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: 'Capminal' }))

    const card = await screen.findByLabelText('Catalog skill morse-launch-b20')
    expect(within(card).getByRole('presentation')).toHaveAttribute('src', capminalSymbolUrl)
  })

  // ── Catalog panel intro copy (one heading per tab, no cross-contamination) ─
  // RegistryPanel used to branch on `group === 'bankr'`, so Capminal fell into
  // the community `else` and advertised itself as a community catalog. Pin the
  // heading of every catalog tab so a fourth source cannot repeat that.
  it.each([
    ['Bankr', 'Bankr skill catalog'],
    ['Capminal', 'Capminal skill catalog'],
  ])('the %s tab shows its own catalog heading, not the community one', async (tab, heading) => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: tab }))

    expect(await screen.findByRole('heading', { name: heading })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Community catalog' })).not.toBeInTheDocument()
    expect(screen.getByRole('searchbox', { name: `Search ${tab} skills` })).toBeInTheDocument()
  })

  // The base skill is a prerequisite for everything else in its catalog, so the
  // warning has to be on the panel itself — a user who reads only the card they
  // are installing would otherwise never see it.
  it.each([
    ['Bankr', "the 'bankr' skill is required for all skills in this catalog."],
    ['Capminal', "the 'capminal' skill is required for all skills in this catalog."],
  ])('the %s tab warns that its base skill is required', async (tab, notice) => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: tab }))

    const note = await screen.findByRole('note')
    expect(note).toHaveTextContent('IMPORTANT:')
    expect(note).toHaveTextContent(notice)
  })

  it('the Community tab keeps the community heading', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))

    expect(await screen.findByRole('heading', { name: 'Community catalog' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /skill catalog$/ })).not.toBeInTheDocument()
  })

  // ── Install (per-item busy, correct RPC + params + invalidation) ─────────
  it('installing a catalog skill calls skills.install with identifier/source/force and reloads', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    const card = await screen.findByLabelText('Catalog skill Uniswap')
    fireEvent.click(within(card).getByRole('button', { name: /^Install$/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('skills.install', {
        identifier: 'uniswap-swap',
        source: 'clawhub',
        force: false,
      }),
    )
    await waitFor(() => expect(toast.success).toHaveBeenCalled())
    // success invalidates skills.list (a reload)
    await waitFor(() => expect(callsFor('skills.list').length).toBeGreaterThanOrEqual(2))
  })

  it('renders an installed catalog chip in the same card-action slot as Install', async () => {
    wireRpc({ searchResults: [{ ...CATALOG_ITEM, installed: true }] })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    const card = await screen.findByLabelText('Catalog skill Uniswap')
    expect(within(card).getByText('Installed')).toHaveClass('sk-chip--card-action')
  })

  it('an overridable refusal arms a force install even when the scan was clean', async () => {
    // A registry `suspicious` verdict or an install-policy `warn` carries no
    // dangerous scan verdict; `overridable` is what says force can answer it.
    wireRpc({
      installResponse: {
        success: false,
        scan_verdict: 'safe',
        policy_decision: 'warn',
        overridable: true,
        name: 'Uniswap',
      },
    })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    const card = await screen.findByLabelText('Catalog skill Uniswap')
    fireEvent.click(within(card).getByRole('button', { name: /^Install$/i }))
    expect(await within(card).findByRole('button', { name: /Force install/i })).toBeInTheDocument()
  })

  it('a dangerous scan verdict arms a force install instead of erroring', async () => {
    wireRpc({
      installResponse: {
        success: false,
        scan_verdict: 'dangerous',
        scan_findings: [1, 2],
        name: 'Uniswap',
      },
    })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    const card = await screen.findByLabelText('Catalog skill Uniswap')
    fireEvent.click(within(card).getByRole('button', { name: /^Install$/i }))
    // Button re-arms to "Force install"; the second click sends force:true.
    const force = await within(card).findByRole('button', { name: /Force install/i })
    fireEvent.click(force)
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('skills.install', {
        identifier: 'uniswap-swap',
        source: 'clawhub',
        force: true,
      }),
    )
  })

  // ── "Use" → chat with the composer prefilled ─────────────────────────────
  // Naming the skill up front (`use skill <name>`) is the documented way to
  // pin the agent to it; the card just saves the operator from retyping it.
  it('the card Use button routes to chat with the skill prompt prefilled', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())

    const card = screen.getByLabelText('Skill trader').closest('article') as HTMLElement
    fireEvent.click(within(card).getByRole('button', { name: /^Use$/i }))

    expect(navigateSpy).toHaveBeenCalledWith('/chat?prompt=use%20skill%20trader%0A')
  })

  it('Use does not also open the skill dialog', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())

    const card = screen.getByLabelText('Skill trader').closest('article') as HTMLElement
    fireEvent.click(within(card).getByRole('button', { name: /^Use$/i }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('the card body still opens the details dialog next to the Use button', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())

    fireEvent.click(screen.getByLabelText('Skill trader'))

    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('Use in chat closes the dialog before routing', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill weather')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Skill weather'))

    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /^Use in chat$/i }))

    expect(navigateSpy).toHaveBeenCalledWith('/chat?prompt=use%20skill%20weather%0A')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  // ── Update / Uninstall from the installed-skill dialog ───────────────────
  it('a managed skill dialog updates via skills.update and reloads', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Skill trader'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /^Update$/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('skills.update', { name: 'trader' }),
    )
    await waitFor(() => expect(callsFor('skills.list').length).toBeGreaterThanOrEqual(2))
  })

  it('a managed skill dialog removes via skills.uninstall and closes', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Skill trader'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /^Remove$/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('skills.uninstall', { name: 'trader' }),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('bundled (non-managed) skill dialog has no Update/Remove', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill weather')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Skill weather'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByRole('button', { name: /^Update$/i })).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /^Remove$/i })).not.toBeInTheDocument()
  })

  // ── deps.install prompt → confirm → deps.install ─────────────────────────
  it('the install-deps button in a skill dialog calls skills.deps.install with name + install_id', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill weather')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Skill weather'))
    const dialog = await screen.findByRole('dialog')
    // The install option surfaces because missing_bins is non-empty.
    fireEvent.click(within(dialog).getByRole('button', { name: /Install via brew/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('skills.deps.install', {
        name: 'weather',
        install_id: 'brew-curl',
      }),
    )
    // Nothing still missing → the dialog closes.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('deps.install that leaves deps missing keeps the dialog open', async () => {
    wireRpc({ depsResponse: { success: true, missing_still: { bins: ['curl'] } } })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill weather')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Skill weather'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /Install via brew/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('skills.deps.install', expect.anything()),
    )
    // Still missing → dialog stays open.
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  // ── Robinhood empty state ────────────────────────────────────────────────
  it('the Robinhood tab shows the empty state when no robinhood skills are installed', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /Robinhood/i }))
    expect(await screen.findByText(/Robinhood skills are on the way/i)).toBeInTheDocument()
    expect(screen.getByLabelText('Search Robinhood skills')).toBeInTheDocument()
  })

  // The Agentic account is provisioned by Robinhood, not by the skill, so the
  // warning belongs on the panel: a user who installs and runs the skill first
  // only learns about it when the first tool call fails.
  it('the Robinhood tab describes the catalog and warns about the Agentic account', async () => {
    wireRpc({ skills: [ROBINHOOD_UNDECLARED, ROBINHOOD_READY] })
    renderPage()
    fireEvent.click(screen.getByRole('tab', { name: /Robinhood/i }))

    expect(await screen.findByRole('heading', { name: 'Robinhood skills' })).toBeInTheDocument()
    expect(screen.getByText(/Robinhood Trading MCP/i)).toBeInTheDocument()
    const note = screen.getByRole('note')
    expect(note).toHaveTextContent('IMPORTANT:')
    expect(note).toHaveTextContent(
      "the 'robinhood-agentic-trading' skill trades from a dedicated Robinhood Agentic account",
    )
    expect(note).toHaveTextContent(/create that account in Robinhood/i)
  })

  it('gives Robinhood the Bankr catalog shell while preserving installed-skill actions', async () => {
    wireRpc({ skills: [ROBINHOOD_UNDECLARED, ROBINHOOD_READY] })
    renderPage()
    fireEvent.click(screen.getByRole('tab', { name: /Robinhood/i }))

    const readyCard = await screen.findByLabelText('Robinhood skill robinhood-rwa-addresses')
    const tradingCard = screen.getByLabelText('Robinhood skill robinhood-agentic-trading')
    expect(readyCard).toHaveClass('sk-rcard', 'sk-rcard--partner')
    expect(readyCard.closest('.sk-grid')).toHaveClass('sk-grid--registry')
    expect(within(readyCard).getByRole('presentation')).toHaveAttribute('src', robinhoodSymbolUrl)
    expect(within(readyCard).getByText('Robinhood')).toBeInTheDocument()
    // The source label is the row's real acquisition now, not a hardcoded
    // 'bundled' — a partner hub install lands in this same tab.
    expect(within(readyCard).getByText('shipped')).toBeInTheDocument()
    expect(within(readyCard).getByText('Ready')).toBeInTheDocument()
    // Declares no requirements, and runs — the card says so rather than
    // parking it in a bucket of its own.
    expect(within(tradingCard).getByText('Ready')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Install$/i })).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Search Robinhood skills'), {
      target: { value: 'addresses' },
    })
    await waitFor(() =>
      expect(
        screen.queryByLabelText('Robinhood skill robinhood-agentic-trading'),
      ).not.toBeInTheDocument(),
    )
    expect(screen.getByLabelText('Robinhood skill robinhood-rwa-addresses')).toBeInTheDocument()
    expect(callsFor('skills.search')).toHaveLength(0)

    fireEvent.change(screen.getByLabelText('Search Robinhood skills'), {
      target: { value: '   ' },
    })
    await waitFor(() =>
      expect(
        screen.getByLabelText('Robinhood skill robinhood-agentic-trading'),
      ).toBeInTheDocument(),
    )
    expect(screen.getByLabelText('Robinhood skill robinhood-rwa-addresses')).toBeInTheDocument()

    fireEvent.click(
      within(readyCard).getByRole('button', {
        name: 'View details for robinhood-rwa-addresses',
      }),
    )
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })

  it('filters Robinhood status locally without changing the partner catalog contract', async () => {
    wireRpc({ skills: [ROBINHOOD_NEEDS, ROBINHOOD_READY] })
    renderPage()
    fireEvent.click(screen.getByRole('tab', { name: /Robinhood/i }))
    await screen.findByLabelText('Robinhood skill robinhood-rwa-addresses')

    fireEvent.click(screen.getByRole('button', { name: 'Filter Robinhood skills: Ready' }))
    expect(screen.getByLabelText('Robinhood skill robinhood-rwa-addresses')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByLabelText('Robinhood skill robinhood-options')).not.toBeInTheDocument(),
    )
    expect(callsFor('skills.search')).toHaveLength(0)
  })

  it('keeps an active zero-count Robinhood status visible after refresh', async () => {
    wireRpc({
      skillsSequence: [[ROBINHOOD_NEEDS, ROBINHOOD_READY], [ROBINHOOD_NEEDS]],
    })
    renderPage()
    fireEvent.click(screen.getByRole('tab', { name: /Robinhood/i }))
    await screen.findByLabelText('Robinhood skill robinhood-rwa-addresses')
    fireEvent.click(screen.getByRole('button', { name: 'Filter Robinhood skills: Ready' }))

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    const readyFilter = await screen.findByRole('button', {
      name: 'Filter Robinhood skills: Ready',
    })
    await waitFor(() => expect(readyFilter).toHaveTextContent('Ready 0'))
    expect(readyFilter).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('No Robinhood skills are ready.')).toBeInTheDocument()
  })

  it('shows a Robinhood skeleton instead of a false empty state while skills load', async () => {
    wireRpc({ listPromise: new Promise(() => undefined) })
    renderPage()
    fireEvent.click(screen.getByRole('tab', { name: /Robinhood/i }))

    expect(
      await screen.findByRole('status', { name: 'Loading Robinhood skills' }),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Robinhood skills are on the way/i)).not.toBeInTheDocument()
  })

  // ── Muse: the second bundled-partner tab ────────────────────────────
  // Same panel as Robinhood, keyed by `publisher.id`. These pin the parts that
  // are per-brand — the tab artwork, the intro copy, the notice, and that the
  // skill lands here and not under Robinhood.
  it('the Muse tab wears its own mark and shows the empty state', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    expect(
      within(screen.getByRole('tab', { name: 'Muse' }))
        .getByRole('presentation')
        .getAttribute('src'),
    ).toBe(museSymbolUrl)

    fireEvent.click(screen.getByRole('tab', { name: /Muse/i }))
    expect(await screen.findByText(/Muse skills are on the way/i)).toBeInTheDocument()
    expect(screen.getByLabelText('Search Muse skills')).toBeInTheDocument()
    expect(screen.queryByText(/Robinhood skills are on the way/i)).not.toBeInTheDocument()
  })

  it('the Muse tab lists only musebook-published skills and warns that boards are public', async () => {
    wireRpc({ skills: [MUSEBOOK_READY, ROBINHOOD_READY] })
    renderPage()
    fireEvent.click(screen.getByRole('tab', { name: /Muse/i }))

    expect(await screen.findByRole('heading', { name: 'Muse skills' })).toBeInTheDocument()
    expect(screen.getByText(/musebook\.lol, the text board/i)).toBeInTheDocument()
    const note = screen.getByRole('note')
    expect(note).toHaveTextContent('IMPORTANT:')
    expect(note).toHaveTextContent(/Posts are permanent/i)
    expect(note).toHaveTextContent(/never links on its own/i)

    const card = await screen.findByLabelText('Muse skill musebook')
    expect(card).toHaveClass('sk-rcard', 'sk-rcard--partner')
    expect(within(card).getByRole('presentation')).toHaveAttribute('src', museSymbolUrl)
    expect(within(card).getByText('Muse')).toBeInTheDocument()
    expect(within(card).getByText('Ready')).toBeInTheDocument()
    expect(
      screen.queryByLabelText('Robinhood skill robinhood-rwa-addresses'),
    ).not.toBeInTheDocument()
    expect(callsFor('skills.search')).toHaveLength(0)
  })

  it('shows the skills load failure inside the Robinhood source panel', async () => {
    wireRpc({ listReject: true })
    renderPage()
    fireEvent.click(screen.getByRole('tab', { name: /Robinhood/i }))

    expect(await screen.findByText(/Failed to load: Error: list down/i)).toBeInTheDocument()
    expect(screen.queryByText(/Robinhood skills are on the way/i)).not.toBeInTheDocument()
  })

  // ── SK1: Requirements + Missing sections in the installed-skill dialog ────
  // (skills.js:743-777,792-803,805,854-855)
  it('the installed-skill dialog renders the Requirements section and Missing bins/env for a needs_setup skill', async () => {
    wireRpc({ skills: [REQ_BUNDLED] })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill oracle')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Skill oracle'))
    const dialog = await screen.findByRole('dialog')

    // Requirements section: heading + one row per requirement item.
    expect(within(dialog).getByText('Requirements')).toBeInTheDocument()
    const reqRows = dialog.querySelectorAll('.sk-dialog__req-row')
    expect(reqRows).toHaveLength(3)
    // Per-requirement name (req-name spans are unique to each row).
    const names = Array.from(dialog.querySelectorAll('.sk-dialog__req-name')).map(
      (n) => n.textContent,
    )
    expect(names).toEqual(['jq', 'apikey', 'python'])
    // Per-requirement status chips (ready / needs setup / missing skill).
    expect(within(dialog).getByText('needs setup')).toBeInTheDocument()
    expect(within(dialog).getByText('missing skill')).toBeInTheDocument()
    expect(within(dialog).getByText('ready')).toBeInTheDocument()
    // Detail: needs_setup row shows "Missing <code>jq</code>"; declared reqs
    // show requires text ("ORACLE_KEY env", "python3").
    const jqRow = reqRows[0]!
    expect(jqRow.querySelector('.sk-dialog__req-detail')?.textContent).toContain('Missing')
    expect(jqRow.querySelector('.sk-dialog__req-detail code')?.textContent).toBe('jq')
    expect(within(dialog).getByText('ORACLE_KEY env')).toBeInTheDocument()
    expect(within(dialog).getByText('python3')).toBeInTheDocument()

    // Missing section: skill-level missing bins + env, each labelled.
    const sectionTitles = Array.from(dialog.querySelectorAll('.sk-dialog__section-title')).map(
      (t) => t.textContent,
    )
    expect(sectionTitles).toContain('Missing')
    expect(within(dialog).getByText('binary')).toBeInTheDocument()
    expect(within(dialog).getByText('env var')).toBeInTheDocument()
    const missingCodes = Array.from(dialog.querySelectorAll('.sk-dialog__missing code')).map(
      (c) => c.textContent,
    )
    expect(missingCodes).toEqual(['jq', 'ORACLE_KEY'])

    // Each entry is its own separated row — the bin and the env var alike — and
    // the env row's action carries the variable name as its accessible name even
    // though the visible label is just "Set".
    expect(dialog.querySelectorAll('.sk-dialog__missing-row')).toHaveLength(2)
    const setButton = within(dialog).getByRole('button', { name: 'Set ORACLE_KEY' })
    expect(setButton).toHaveTextContent('Set')
    expect(setButton.closest('.sk-dialog__missing-row')).not.toBeNull()
  })

  // ── SK2: category badge on the registry card + detail dialog ──────────────
  // (skills.js:659-660,670,685)
  it('the registry card and detail dialog show the category badge (CAT_LABEL)', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    const card = await screen.findByLabelText('Catalog skill Uniswap')
    // 'defi' → 'DeFi' label on the card.
    expect(within(card).getByText('DeFi')).toBeInTheDocument()
    // Open the detail dialog: the category chip renders there too.
    fireEvent.click(within(card).getByRole('button', { name: 'View details for Uniswap' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('DeFi')).toBeInTheDocument()
  })

  // ── SK3: Demo section heading carries the demo title + language ───────────
  // (skills.js:702-708)
  it('the registry detail Demo section shows the demo title and language labels', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    const card = await screen.findByLabelText('Catalog skill Uniswap')
    fireEvent.click(within(card).getByRole('button', { name: 'View details for Uniswap' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/Demo/)).toBeInTheDocument()
    // Title + language render as their own labelled spans in the heading.
    expect(within(dialog).getByText('Quote a swap')).toBeInTheDocument()
    expect(within(dialog).getByText('python')).toBeInTheDocument()
  })

  // ── #130: the Installed tab groups on provenance, not on the layer ───────
  it('groups the Installed tab into Partner / Normal / Hub / Local', async () => {
    wireRpc({
      skills: [READY_MANAGED, NEEDS_BUNDLED, LOCAL_IN_MANAGED, BANKR_HUB_SKILL, ROBINHOOD_READY],
    })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())

    expect(screen.getAllByRole('heading').map((h) => h.textContent)).toEqual(
      expect.arrayContaining([
        'Partner Skills',
        'AgentOS Normal Skills',
        'Installed from a hub',
        'Your local skills',
      ]),
    )
    // The issue's stated criterion: Bankr and Robinhood under ONE heading,
    // even though one is a hub install and the other ships with AgentOS.
    const partners = groupNamed('Partner Skills')
    expect(within(partners).getByLabelText('Skill bankr-swaps')).toBeInTheDocument()
    expect(within(partners).getByLabelText('Skill robinhood-rwa-addresses')).toBeInTheDocument()

    expect(
      within(groupNamed('Installed from a hub')).getByLabelText('Skill trader'),
    ).toBeInTheDocument()
    expect(
      within(groupNamed('AgentOS Normal Skills')).getByLabelText('Skill weather'),
    ).toBeInTheDocument()
    // layer 'managed' with no lockfile entry is a local skill, not a hub one.
    expect(
      within(groupNamed('Your local skills')).getByLabelText('Skill handrolled'),
    ).toBeInTheDocument()
  })

  it('keeps the layer visible as a per-card detail chip', async () => {
    wireRpc({ skills: [READY_MANAGED] })
    renderPage()
    const card = await screen.findByLabelText('Skill trader')
    expect(within(card).getByText('Managed')).toBeInTheDocument()
  })

  // ── A community skill served by a partner's hub keeps its origin ─────────
  it('credits the hub and the author of an unbranded hub install', async () => {
    wireRpc({ skills: [COMMUNITY_HUB_SKILL] })
    renderPage()
    const card = await screen.findByLabelText('Skill stranger-skill')

    // It stays out of Partner Skills — the credit must not smuggle a brand in.
    expect(
      within(groupNamed('Installed from a hub')).getByLabelText('Skill stranger-skill'),
    ).toBeInTheDocument()
    const origin = within(card).getByTitle('Installed from clawhub, authored by @somebody')
    expect(origin).toHaveTextContent('clawhub')
    expect(origin).toHaveTextContent('@somebody')
    // Attribution only: no partner artwork anywhere on the card.
    expect(within(card).queryByRole('img')).not.toBeInTheDocument()
  })

  // The catalog tabs render the brand mark, so an installed partner skill that
  // fell back to the generic package glyph read as a different skill entirely.
  it('shows the publisher brand mark on an installed partner skill', async () => {
    wireRpc({ skills: [WALLET_PARTNER_SKILL] })
    renderPage()
    const card = await screen.findByLabelText('Skill stock-premium-lp-manager')
    expect(within(card).getByRole('img', { name: 'Bankr logo' })).toBeInTheDocument()

    fireEvent.click(card)
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('img', { name: 'Bankr logo' })).toBeInTheDocument()
  })

  it('leaves a skill with no allowlisted publisher on the generic glyph', async () => {
    wireRpc({ skills: [COMMUNITY_HUB_SKILL] })
    renderPage()
    const card = await screen.findByLabelText('Skill stranger-skill')
    expect(within(card).queryByRole('img')).not.toBeInTheDocument()
  })

  // The group heading already carries the AgentOS name; the card wearing the
  // generic package glyph made an AgentOS-authored skill look anonymous next to
  // the branded partner rows.
  it('shows the AgentOS mark on an AgentOS crypto skill without making it a partner', async () => {
    wireRpc({ skills: [AGENTOS_CRYPTO_SKILL] })
    renderPage()
    const card = await screen.findByLabelText('Skill senior-unilp-manager')
    expect(within(card).getByRole('img', { name: 'AgentOS logo' })).toBeInTheDocument()

    // It stays under its own heading — the mark must not smuggle it into Partners.
    expect(
      within(groupNamed('AgentOS Crypto Skills')).getByLabelText('Skill senior-unilp-manager'),
    ).toBeInTheDocument()

    fireEvent.click(card)
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('img', { name: 'AgentOS logo' })).toBeInTheDocument()
  })

  // Seven GMGN skills under one heading all wearing the generic AgentOS mark
  // read as one skill listed seven times.
  it('gives a GMGN skill the GMGN mark badged with its own emoji', async () => {
    wireRpc({ skills: [GMGN_SKILL, AGENTOS_CRYPTO_SKILL] })
    renderPage()
    const card = await screen.findByLabelText('Skill gmgn-token')
    expect(within(card).getByRole('img', { name: 'GMGN logo' })).toBeInTheDocument()
    expect(within(card).getByText('🔎')).toBeInTheDocument()

    // It shares the crypto heading with the AgentOS-marked skill rather than
    // splitting off, and the mark does not promote it to a partner.
    const group = groupNamed('AgentOS Crypto Skills')
    expect(within(group).getByLabelText('Skill gmgn-token')).toBeInTheDocument()
    expect(within(group).getByLabelText('Skill senior-unilp-manager')).toBeInTheDocument()
    expect(
      within(within(group).getByLabelText('Skill senior-unilp-manager')).getByRole('img', {
        name: 'AgentOS logo',
      }),
    ).toBeInTheDocument()

    fireEvent.click(card)
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('img', { name: 'GMGN logo' })).toBeInTheDocument()
  })

  it('files an allowlisted wallet skill under its partner and still credits the author', async () => {
    wireRpc({ skills: [WALLET_PARTNER_SKILL] })
    renderPage()
    const card = await screen.findByLabelText('Skill stock-premium-lp-manager')

    expect(
      within(groupNamed('Partner Skills')).getByLabelText('Skill stock-premium-lp-manager'),
    ).toBeInTheDocument()
    // The brand already says "bankr", so the chip drops the source id and keeps
    // only the part the publisher record cannot carry.
    const origin = within(card).getByTitle('Installed from bankr, authored by @igoryuzo')
    expect(origin).toHaveTextContent('@igoryuzo')
    expect(origin).not.toHaveTextContent(/bankr/i)
  })

  it('names the hub but no author when the catalog credited none', async () => {
    wireRpc({ skills: [READY_MANAGED] })
    renderPage()
    const card = await screen.findByLabelText('Skill trader')
    expect(within(card).getByTitle('Installed from clawhub')).toHaveTextContent('clawhub')
  })

  it('shows no origin chip for a skill that shipped with AgentOS', async () => {
    wireRpc({ skills: [NEEDS_BUNDLED] })
    renderPage()
    const card = await screen.findByLabelText('Skill weather')
    expect(within(card).queryByTitle(/^Installed from/)).not.toBeInTheDocument()
  })

  // ── #130: Update/Remove come off the payload, not off the layer ──────────
  it('a managed-layer skill with no lockfile entry offers neither Update nor Remove', async () => {
    wireRpc({ skills: [LOCAL_IN_MANAGED] })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill handrolled')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Skill handrolled'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByRole('button', { name: /^Update$/i })).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /^Remove$/i })).not.toBeInTheDocument()
  })

  it('a hub skill AgentOS cannot delete explains itself instead of offering a dead Remove', async () => {
    wireRpc({ skills: [STRANDED_HUB] })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill stranded')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Skill stranded'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('button', { name: /^Update$/i })).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /^Remove$/i })).not.toBeInTheDocument()
    expect(within(dialog).getByText(/cannot remove this skill/i)).toBeInTheDocument()
  })

  // ── #130 core: installed, ready, and still not reaching the agent ────────
  it('a skill the agent is never offered says so on the card and why in the dialog', async () => {
    wireRpc({ skills: [WITHHELD_SKILL, READY_MANAGED] })
    renderPage()
    const card = await screen.findByLabelText('Skill quiet')
    // The status is untouched — this is a third state, not a failed one.
    expect(within(card).getByText('Ready')).toBeInTheDocument()
    expect(within(card).getByText('Not offered — agent cannot invoke')).toBeInTheDocument()
    // A skill that IS offered carries no such chip.
    expect(
      within(screen.getByLabelText('Skill trader')).queryByText(/Not offered/),
    ).not.toBeInTheDocument()

    fireEvent.click(card)
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Agent availability')).toBeInTheDocument()
    expect(within(dialog).getByText(WITHHELD_SKILL.availability.detail)).toBeInTheDocument()
  })

  // ── #130 symptom 2: the Community list stops swapping ────────────────────
  it('a row installed while searching survives the search being cleared', async () => {
    wireRpc({
      search: (query) => (query ? [SEARCH_ONLY_ITEM] : [CATALOG_ITEM]),
      installResponse: { success: true, name: 'ledger-watch' },
    })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    const input = await screen.findByLabelText('Search community skills')

    fireEvent.change(input, { target: { value: 'ledger' } })
    const card = await screen.findByLabelText('Catalog skill Ledger Watch')
    fireEvent.click(within(card).getByRole('button', { name: /^Install$/i }))
    await waitFor(() => expect(toast.success).toHaveBeenCalled())

    fireEvent.change(input, { target: { value: '' } })
    // The browse list is back…
    await screen.findByLabelText('Catalog skill Uniswap')
    // …and the row the browse itself never returned is still on it, installed.
    const stillThere = screen.getByLabelText('Catalog skill Ledger Watch')
    expect(within(stillThere).getByText('Installed')).toBeInTheDocument()
  })

  it('clearing the search does not unmount an open catalog dialog', async () => {
    wireRpc({ search: (query) => (query ? [SEARCH_ONLY_ITEM] : [CATALOG_ITEM]) })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    const input = await screen.findByLabelText('Search community skills')

    fireEvent.change(input, { target: { value: 'ledger' } })
    const card = await screen.findByLabelText('Catalog skill Ledger Watch')
    fireEvent.click(within(card).getByRole('button', { name: 'View details for Ledger Watch' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()

    fireEvent.change(input, { target: { value: '' } })
    await screen.findByLabelText('Catalog skill Uniswap')
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Ledger Watch')).toBeInTheDocument()
  })

  it('uninstalling refreshes the catalog so the Installed chip is not stale', async () => {
    let removed = false
    wireRpc({
      skills: [READY_MANAGED],
      search: () => [{ ...CATALOG_ITEM, installed: !removed }],
    })
    const client = makeClient()
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    renderPage(client)

    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    const before = await screen.findByLabelText('Catalog skill Uniswap')
    expect(within(before).getByText('Installed')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'Installed' }))
    fireEvent.click(await screen.findByLabelText('Skill trader'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /^Remove$/i }))
    // The gateway has dropped the lockfile entry the chip is derived from.
    removed = true
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['skills.search'] }))

    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    const after = await screen.findByLabelText('Catalog skill Uniswap')
    await waitFor(() =>
      expect(within(after).getByRole('button', { name: /^Install$/i })).toBeInTheDocument(),
    )
  })

  it('renders large installed chip in the detail dialog footer for an installed skill', async () => {
    wireRpc({
      search: () => [{ ...CATALOG_ITEM, installed: true }],
    })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    const card = await screen.findByLabelText('Catalog skill Uniswap')
    fireEvent.click(within(card).getByRole('button', { name: 'View details for Uniswap' }))
    const dialog = await screen.findByRole('dialog')
    const installedChip = within(dialog).getByText('Installed')
    expect(installedChip).toHaveClass('sk-chip', 'sk-chip--ok', 'sk-chip--lg')
  })

  it('a failed live search shows the error instead of an empty catalog', async () => {
    wireRpc({
      search: (query) => (query ? Promise.reject(new Error('search down')) : [CATALOG_ITEM]),
    })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('tab', { name: /^Community$/i }))
    const input = await screen.findByLabelText('Search community skills')

    fireEvent.change(input, { target: { value: 'zzz' } })
    expect(await screen.findByText(/Failed to load: Error: search down/)).toBeInTheDocument()
    expect(screen.queryByText('No skills match zzz.')).not.toBeInTheDocument()
  })

  it('sets the document title', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(document.title).toBe('Skills - AgentOS Control'))
  })

  it('loads and searches Capminal skills when entering the Capminal tab', async () => {
    wireRpc({
      searchResults: [
        {
          name: 'capminal',
          identifier: 'https://github.com/capminal-skills/capminal',
          provider: 'Capminal',
          source: 'capminal',
          description: 'Cap World interaction',
          category: 'crypto',
        },
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Skill trader')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('tab', { name: 'Capminal' }))

    const card = await screen.findByLabelText('Catalog skill capminal')
    expect(card).toBeInTheDocument()
    expect(within(card).getByText('Cap World interaction')).toBeInTheDocument()
    expect(within(card).getByText('Crypto')).toBeInTheDocument()

    fireEvent.click(within(card).getByRole('button', { name: 'View details for capminal' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Capminal')).toBeInTheDocument()
    expect(within(dialog).getByText('Crypto')).toBeInTheDocument()

    expect(mockRpc.call).toHaveBeenCalledWith('skills.search', {
      query: '',
      limit: 500,
      source: 'capminal',
    })
  })
})
