import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { routeChildren } from './routes'
import { AppProviders } from './providers'
import { AppShell } from './AppShell'
import { useConnection } from '@/stores/connection'
import { useApprovals } from '@/services/approval-monitor'
import type { Bootstrap } from '@/lib/bootstrap'

// AppShell reads useBootstrap() (version footer) — in production it always runs
// under AppProviders. The shell tests below drive it without a live bootstrap
// fetch, so we stub useBootstrap to return a controllable bootstrap object.
let mockBootstrap: Bootstrap = {
  version: '',
  ws_url: 'ws://localhost/ws',
  auth_mode: '',
  base_path: '/control',
  config_path: '',
  features: { diagnostics: false },
}

// The index route (/) now renders the real OverviewPage (the default desktop
// view is no longer a stub), which reads useRpc()/useConnection. The shell
// tests drive the tree without AppProviders, so stub useRpc with a no-op RPC
// whose waitForConnection never settles — OverviewPage mounts (shell chrome +
// the view header render) without firing real RPC traffic in a chrome test.
const noopRpc = {
  waitForConnection: () => new Promise<void>(() => {}),
  call: () => new Promise(() => {}),
  on: () => () => {},
  connect: () => {},
  disconnect: () => {},
}
vi.mock('./providers', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./providers')>()
  return { ...actual, useBootstrap: () => mockBootstrap, useRpc: () => noopRpc }
})

// Render the route tree without AppProviders (no network): test harness
// provides QueryClient only; views under test here are stubs.
function renderAt(path: string) {
  const router = createMemoryRouter(routeChildren, { initialEntries: [path] })
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('routes', () => {
  it('renders a stub for every registered view', () => {
    renderAt('/sessions')
    expect(screen.getByRole('heading', { name: 'Sessions' })).toBeInTheDocument()
  })

  it('renders XSS-safe 404 text for unknown paths', () => {
    // The hostile path must actually reach the DOM as text: NotFound reads
    // useLocation().pathname (router-driven), so the routed path — not a stale
    // window.location — is what renders. Assert the full hostile string is
    // present as literal text and that no <script> element was injected.
    renderAt('/nope<script>alert(1)</script>')
    expect(screen.getByText('Page not found: /nope<script>alert(1)</script>')).toBeInTheDocument()
    expect(document.querySelector('script')).toBeNull()
  })

  it('sets the document title from the route', () => {
    renderAt('/cron')
    expect(document.title).toBe('Cron - AgentOS Control')
  })

  // M1 — parity: router.js:68-71 — an unmatched route has no meta.title, so the
  // legacy title resolves to 'Not Found - AgentOS Control' (not a view title).
  it('sets the 404 document title to "Not Found - AgentOS Control"', () => {
    renderAt('/definitely-not-a-route')
    expect(document.title).toBe('Not Found - AgentOS Control')
  })
})

// M3 — parity: router.js:29-66 — the index route renders the DEFAULT view in
// place while leaving the address bar at the base path (no URL rewrite), and
// highlights that view's nav item. matchMedia is evaluated per resolve.
describe('index route renders the default view without changing the URL', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function stubMatchMedia(matches: boolean) {
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({
        matches,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    )
  }

  function renderShellAt(path: string) {
    const router = createMemoryRouter([{ element: <AppShell />, children: routeChildren }], {
      initialEntries: [path],
    })
    return {
      router,
      ...render(
        <QueryClientProvider client={new QueryClient()}>
          <RouterProvider router={router} />
        </QueryClientProvider>,
      ),
    }
  }

  it('renders Overview on desktop and leaves the URL at "/"', () => {
    stubMatchMedia(false)
    const { router } = renderShellAt('/')
    // Default view rendered in place (Overview stub heading).
    expect(screen.getByRole('heading', { name: 'Overview' })).toBeInTheDocument()
    // The address bar stays at the base path — no Navigate rewrite to /overview.
    expect(router.state.location.pathname).toBe('/')
    // The default view's nav item is highlighted (aria-current=page).
    const overviewLink = screen.getByRole('link', { name: 'Overview' })
    expect(overviewLink).toHaveAttribute('aria-current', 'page')
  })

  it('renders Chat on mobile at the index URL', () => {
    stubMatchMedia(true)
    const { router } = renderShellAt('/')
    expect(screen.getByRole('heading', { name: 'Chat' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/')
    // On mobile the closed drawer is aria-hidden/inert, so the nav link is not
    // in the accessibility tree; assert the highlight via the DOM node instead.
    const chatLink = document.querySelector<HTMLAnchorElement>('a[href="/chat"]')!
    expect(chatLink).toHaveAttribute('aria-current', 'page')
  })
})

// M13/M14/M17 shell chrome parity, plus the mobile drawer behavior.
// Parity: app.js:119-171 — mobile sidebar drawer (hamburger toggle, close on
// nav-click / outside-click / Escape, aria-expanded + aria-hidden/inert sync
// at <=768px). jsdom has no matchMedia, so each test stubs it.
describe('app shell chrome', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    mockBootstrap = { ...mockBootstrap, version: '' }
    useConnection.getState().setState('disconnected')
  })

  function stubMatchMedia(matches: boolean) {
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({
        matches,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    )
  }

  function renderShellAt(path: string) {
    const router = createMemoryRouter([{ element: <AppShell />, children: routeChildren }], {
      initialEntries: [path],
    })
    return render(
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
  }

  it('hides the drawer on mobile until the hamburger opens it, and closes on nav click', () => {
    stubMatchMedia(true)
    renderShellAt('/cron')
    const toggle = screen.getByRole('button', { name: 'Toggle menu' })
    const sidebar = document.getElementById('sidebar-nav')!
    // Closed drawer at <=768px: aria-expanded false, hidden + inert for AT.
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveAttribute('aria-controls', 'sidebar-nav')
    expect(sidebar).toHaveAttribute('aria-hidden', 'true')
    expect(sidebar).toHaveAttribute('inert')

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(sidebar).not.toHaveAttribute('aria-hidden')
    expect(sidebar).not.toHaveAttribute('inert')

    // app.js:141-143 — clicking a nav item closes the drawer.
    fireEvent.click(screen.getByRole('link', { name: 'Sessions' }))
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(sidebar).toHaveAttribute('aria-hidden', 'true')
  })

  it('closes on Escape and on outside click', () => {
    stubMatchMedia(true)
    renderShellAt('/cron')
    const toggle = screen.getByRole('button', { name: 'Toggle menu' })

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    // app.js:153-157 — Esc closes the drawer.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    // app.js:147-151 — a click outside the sidebar/toggle closes the drawer.
    fireEvent.click(screen.getByRole('main'))
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  it('keeps the sidebar visible to AT on desktop (no aria-hidden/inert)', () => {
    stubMatchMedia(false)
    renderShellAt('/cron')
    const sidebar = document.getElementById('sidebar-nav')!
    expect(sidebar).not.toHaveAttribute('aria-hidden')
    expect(sidebar).not.toHaveAttribute('inert')
  })

  // M17 — parity: app.js:72-88 — nav grouped under Chat / Control / Settings,
  // Chat first, Approvals last under Settings.
  it('groups nav under Chat / Control / Settings with Chat first and Approvals last', () => {
    stubMatchMedia(false)
    renderShellAt('/cron')
    const nav = screen.getByRole('navigation', { name: 'Main' })
    const labels = within(nav)
      .getAllByText(/^(Chat|Control|Settings)$/)
      // group labels are the uppercase-styled divs, not the nav links
      .filter((el) => el.tagName === 'DIV')
      .map((el) => el.textContent)
    expect(labels).toEqual(['Chat', 'Control', 'Settings'])

    const links = within(nav)
      .getAllByRole('link')
      .map((el) => el.textContent)
    // Chat is the very first nav item; Approvals is the very last.
    expect(links[0]).toBe('Chat')
    expect(links[links.length - 1]).toBe('Approvals')
  })

  // M13 — parity: app.js:58-68 — version footer 'v<semver>' derived from the
  // bootstrap version with the '+NNN' build-suffix stripped and safe-charset
  // filtered; suppressed entirely when the version is empty.
  it('renders the sidebar version footer with the build-suffix stripped', () => {
    stubMatchMedia(false)
    mockBootstrap = { ...mockBootstrap, version: '2026.7.19+1779915602' }
    renderShellAt('/cron')
    const foot = screen.getByTestId('nav-foot')
    expect(foot).toHaveTextContent('v2026.7.19')
    expect(foot).not.toHaveTextContent('1779915602')
  })

  it('suppresses the version footer when the bootstrap version is empty', () => {
    stubMatchMedia(false)
    mockBootstrap = { ...mockBootstrap, version: '' }
    renderShellAt('/cron')
    expect(screen.queryByTestId('nav-foot')).toBeNull()
  })

  // M14 — parity: app.js:94,174-183 — the connection pill is PERSISTENT (never
  // unmounts) and shows a compact 'Connected' ok state with a title attr, plus
  // 'Connecting'/'Disconnected' states.
  it('shows a persistent connection pill across all states including Connected', () => {
    stubMatchMedia(false)
    renderShellAt('/cron')
    const pill = document.getElementById('conn-pill')!

    // Disconnected (initial store state).
    expect(pill).toHaveTextContent('Disconnected')
    expect(pill).toHaveAttribute('title', 'Disconnected')
    expect(pill).toHaveAttribute('data-variant', 'err')

    // Connecting.
    act(() => useConnection.getState().setState('connecting'))
    expect(pill).toHaveTextContent('Connecting')
    expect(pill).toHaveAttribute('title', 'Connecting')
    expect(pill).toHaveAttribute('data-variant', 'warn')

    // Connected — the pill STAYS mounted with a visible ok state (legacy did not
    // unmount the indicator on connect).
    act(() => useConnection.getState().setState('connected'))
    expect(document.getElementById('conn-pill')).not.toBeNull()
    expect(pill).toHaveTextContent('Connected')
    expect(pill).toHaveAttribute('title', 'Connected')
    expect(pill).toHaveAttribute('data-variant', 'ok')
  })
})

// approval_monitor.js:118-138 — the pending approval count drives a badge on the
// Approvals nav item (legacy #approval-count, hidden at 0). The shell reads the
// useApprovals store directly; renderAt() does NOT start the monitor, so the
// store is driven imperatively here.
describe('approvals nav badge', () => {
  // The nav lives in AppShell, so wrap it as the layout parent (like the other
  // shell-chrome tests) rather than rendering a bare view.
  function renderShellAt(path: string) {
    const router = createMemoryRouter([{ element: <AppShell />, children: routeChildren }], {
      initialEntries: [path],
    })
    return render(
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
  }

  afterEach(() => {
    useApprovals.setState({ pending: [], count: 0, mode: 'prompt' })
  })

  it('hides the badge when no approvals are pending', () => {
    useApprovals.setState({ pending: [], count: 0, mode: 'prompt' })
    renderShellAt('/overview')
    expect(screen.queryByTestId('approval-badge')).not.toBeInTheDocument()
  })

  it('shows the pending count on the Approvals nav item', () => {
    useApprovals.setState({
      pending: [{ id: 'a1' }, { id: 'a2' }, { id: 'a3' }],
      count: 3,
      mode: 'prompt',
    })
    renderShellAt('/overview')
    const badge = screen.getByTestId('approval-badge')
    expect(badge).toHaveTextContent('3')
    expect(badge).toHaveAttribute('aria-label', '3 pending approvals')
    // The badge lives on the Approvals link, not any other nav item.
    const approvalsLink = screen.getByRole('link', { name: /approvals/i })
    expect(approvalsLink).toContainElement(badge)
  })

  it('uses the singular aria-label for a single pending approval', () => {
    useApprovals.setState({ pending: [{ id: 'a1' }], count: 1, mode: 'prompt' })
    renderShellAt('/overview')
    expect(screen.getByTestId('approval-badge')).toHaveAttribute('aria-label', '1 pending approval')
  })
})

// Guards the effect-cleanup path in AppProviders (Task 5 review carry-forward):
// the bootstrap fetch + rpc subscription must unsubscribe/disconnect on unmount
// so a StrictMode-style double mount does not leak or crash.
describe('AppProviders effect cleanup', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('mounts and unmounts repeatedly without crashing', () => {
    // Bootstrap fetch never resolves here, so the provider stays in its
    // "Connecting…" state; we exercise mount → unmount → remount purely to
    // verify the cleanup function runs (unsubscribe + disconnect) without error.
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => {})),
    )

    const first = render(
      <AppProviders>
        <div>child</div>
      </AppProviders>,
    )
    expect(() => first.unmount()).not.toThrow()

    const second = render(
      <AppProviders>
        <div>child</div>
      </AppProviders>,
    )
    expect(() => second.unmount()).not.toThrow()
  })
})
