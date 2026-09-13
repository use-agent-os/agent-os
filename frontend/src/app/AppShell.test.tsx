import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getViews, routeChildren } from './routes'
import { AppProviders } from './providers'
import '@/views/chat/ChatPage'
import '@/views/overview/OverviewPage'
import '@/components/ShortcutOverlay'
import {
  AppShell,
  SIDEBAR_COLLAPSED_STORAGE_KEY,
  DISMISSED_VERSION_STORAGE_KEY,
  NAV_SHORTCUTS,
} from './AppShell'
import { KeyboardShortcutProvider } from '@/components/KeyboardShortcuts'
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
  features: { diagnostics: false },
}

// The index route (/) now renders the real OverviewPage (the default desktop
// view is no longer a stub), which reads useRpc()/useConnection. The shell
// tests drive the tree without AppProviders, so stub useRpc with a no-op RPC
// whose waitForConnection never settles — OverviewPage mounts (shell chrome +
// the view header render) without firing real RPC traffic in a chrome test.
const mockRpcCall = vi.fn()
const noopRpc = {
  waitForConnection: () => new Promise<void>(() => {}),
  call: mockRpcCall,
  on: () => () => {},
  connect: () => {},
  disconnect: () => {},
}
vi.mock('./providers', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./providers')>()
  return { ...actual, useBootstrap: () => mockBootstrap, useRpc: () => noopRpc }
})

beforeEach(() => {
  mockRpcCall.mockReset()
  mockRpcCall.mockImplementation(() => new Promise(() => {}))
})

// Render the route tree without AppProviders (no network): test harness
// provides QueryClient only; lazy route modules still resolve normally.
function renderAt(path: string) {
  const router = createMemoryRouter(routeChildren, { initialEntries: [path] })
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('routes', () => {
  it('renders a real lazily loaded registered view', async () => {
    renderAt('/sessions')
    expect(await screen.findByRole('heading', { name: 'Sessions' })).toBeInTheDocument()
  })

  it('registers every major view as a route-object lazy module', () => {
    const views = getViews()
    const registered = routeChildren.filter(
      (route) => typeof route.path === 'string' && views.some((view) => view.path === route.path),
    )

    expect(registered).toHaveLength(views.length)
    expect(registered.every((route) => route.lazy != null)).toBe(true)
    expect(registered.every((route) => route.element == null && route.Component == null)).toBe(true)
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

  it('sets the document title from the route', async () => {
    renderAt('/cron')
    await waitFor(() => expect(document.title).toBe('Cron - AgentOS Control'))
  })

  // M1 — parity: router.js:68-71 — an unmatched route has no meta.title, so the
  // legacy title resolves to 'Not Found - AgentOS Control' (not a view title).
  it('sets the 404 document title to "Not Found - AgentOS Control"', () => {
    renderAt('/definitely-not-a-route')
    expect(document.title).toBe('Not Found - AgentOS Control')
  })

  it('aligns navigation shortcut paths with registered view routes', () => {
    const views = getViews()
    for (const shortcut of NAV_SHORTCUTS) {
      expect(views.some((view) => view.path === shortcut.path)).toBe(true)
    }
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

  it('renders Overview on desktop and leaves the URL at "/"', async () => {
    stubMatchMedia(false)
    const { router } = renderShellAt('/')
    expect(await screen.findByRole('heading', { name: 'Overview' })).toBeInTheDocument()
    // The address bar stays at the base path — no Navigate rewrite to /overview.
    expect(router.state.location.pathname).toBe('/')
    // The default view's nav item is highlighted (aria-current=page).
    const overviewLink = screen.getByRole('link', { name: 'Overview' })
    expect(overviewLink).toHaveAttribute('aria-current', 'page')
  })

  it('renders Chat on mobile at the index URL', async () => {
    stubMatchMedia(true)
    const { router } = renderShellAt('/')
    // The real ChatPage renders in place (its full-bleed thread region), not the
    // Overview view — proving the mobile index default resolves to chat.
    await waitFor(() => expect(document.querySelector('.chat-thread')).not.toBeNull())
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
    window.localStorage.removeItem(SIDEBAR_COLLAPSED_STORAGE_KEY)
    window.localStorage.removeItem(DISMISSED_VERSION_STORAGE_KEY)
    document.body.style.overflow = ''
    document.querySelector('base[data-test-skip-link]')?.remove()
    mockBootstrap = { ...mockBootstrap, version: '' }
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
    expect(sidebar).toHaveAttribute('role', 'dialog')
    expect(sidebar).toHaveAttribute('aria-modal', 'true')
    expect(screen.getByRole('link', { name: 'Cron' })).toHaveFocus()
    expect(document.querySelector('.shell-workspace')).toHaveAttribute('inert')

    // app.js:141-143 — clicking a nav item closes the drawer.
    fireEvent.click(screen.getByRole('link', { name: 'Sessions' }))
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(sidebar).toHaveAttribute('aria-hidden', 'true')
  })

  it('contains keyboard focus in the mobile drawer and restores its trigger', () => {
    stubMatchMedia(true)
    renderShellAt('/cron')
    const toggle = screen.getByRole('button', { name: 'Toggle menu' })
    fireEvent.click(toggle)

    const sidebar = document.getElementById('sidebar-nav')!
    const activeLink = screen.getByRole('link', { name: 'Cron' })
    const theme = within(sidebar).getByRole('button', { name: /theme:/i })
    expect(activeLink).toHaveFocus()

    theme.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(screen.getByRole('link', { name: 'Chat' })).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(theme).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(toggle).toHaveFocus()
    expect(document.querySelector('.shell-workspace')).not.toHaveAttribute('inert')
  })

  it('closes on Escape and on outside click', () => {
    stubMatchMedia(true)
    renderShellAt('/cron')
    const toggle = screen.getByRole('button', { name: 'Toggle menu' })

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: 'Close navigation' })).toBeInTheDocument()
    expect(document.body.style.overflow).toBe('hidden')
    // app.js:153-157 — Esc closes the drawer.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveFocus()
    expect(document.body.style.overflow).toBe('')

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    // app.js:147-151 — a click outside the sidebar/toggle closes the drawer.
    fireEvent.click(document.querySelector('main')!)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(toggle)
    fireEvent.click(screen.getByRole('button', { name: 'Close navigation' }))
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  it('keeps the sidebar visible to AT on desktop (no aria-hidden/inert)', () => {
    stubMatchMedia(false)
    renderShellAt('/cron')
    const sidebar = document.getElementById('sidebar-nav')!
    expect(sidebar).not.toHaveAttribute('aria-hidden')
    expect(sidebar).not.toHaveAttribute('inert')
  })

  it('places the single New chat action in the floating Chat header', async () => {
    stubMatchMedia(false)
    renderShellAt('/chat')

    const sidebar = document.getElementById('sidebar-nav')!
    const header = await screen.findByTestId('shell-chat-header')
    const slot = await screen.findByTestId('shell-chat-header-primary-action')
    const action = await screen.findByRole('button', { name: 'New chat' })
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(slot).toContainElement(action)
    expect(header).toContainElement(action)
    expect(sidebar).not.toContainElement(action)
    expect(nav).not.toContainElement(action)
    expect(document.querySelector('.chat-composer')).not.toContainElement(action)
    expect(screen.getAllByRole('button', { name: 'New chat' })).toHaveLength(1)
    // The tooltip carries a platform-dependent shortcut hint (⌘⇧O vs
    // Ctrl+Shift+O), so match the stable prefix instead of the literal string.
    expect(action.getAttribute('title')).toMatch(/^New chat/)
    expect(action.querySelector('svg')).not.toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Collapse navigation' }))
    expect(sidebar).toHaveAttribute('data-collapsed', 'true')
    expect(action).toBeInTheDocument()
  })

  it('keeps New chat in the mobile Chat header and focuses Message after use', async () => {
    stubMatchMedia(true)
    renderShellAt('/chat')
    const toggle = screen.getByRole('button', { name: 'Toggle menu' })

    const action = await screen.findByRole('button', { name: 'New chat' })
    expect(screen.getByTestId('shell-chat-header')).toContainElement(action)
    fireEvent.click(action)

    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(document.getElementById('sidebar-nav')).toHaveAttribute('inert')
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveFocus()
  })

  it('renders a floating sidebar and folds desktop navigation into an accessible icon rail', () => {
    stubMatchMedia(false)
    renderShellAt('/cron')

    const sidebar = document.getElementById('sidebar-nav')!
    const sidebarHead = sidebar.querySelector('.shell-sidebar__head')!
    expect(sidebarHead).toHaveClass('h-16')
    expect(screen.queryByTestId('shell-header')).not.toBeInTheDocument()
    expect(sidebar.querySelector('.shell-sidebar__brand-mark')).not.toBeNull()
    expect(sidebar).toHaveAttribute('data-collapsed', 'false')

    const collapse = screen.getByRole('button', { name: 'Collapse navigation' })
    expect(collapse).toHaveAttribute('aria-controls', 'sidebar-nav')
    expect(collapse).toHaveAttribute('aria-expanded', 'true')
    expect(collapse.querySelector('svg')).toHaveClass('lucide-chevron-left')
    fireEvent.click(collapse)

    expect(sidebar).toHaveAttribute('data-collapsed', 'true')
    expect(window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe('true')
    expect(screen.getByRole('link', { name: 'Cron' })).toHaveAttribute('aria-current', 'page')

    const expand = screen.getByRole('button', { name: 'Expand navigation' })
    expect(expand).toHaveAttribute('aria-expanded', 'false')
    expect(expand.querySelector('svg')).toHaveClass('lucide-chevron-right')
    fireEvent.click(expand)
    expect(sidebar).toHaveAttribute('data-collapsed', 'false')
    expect(window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe('false')
  })

  it('uses one modern shell while keeping route-specific page surfaces isolated', () => {
    stubMatchMedia(false)
    const control = renderShellAt('/cron')

    const controlShell = document.querySelector('.shell')!
    const controlView = document.querySelector('.view-container')!
    expect(controlShell).toHaveAttribute('data-surface', 'control')
    expect(controlShell).toHaveAttribute('data-design', 'unified')
    expect(controlView).toHaveClass('control-surface')
    expect(controlView).toHaveClass('view-enter')
    expect(within(controlView as HTMLElement).getByTestId('control-header-signal')).toHaveAttribute(
      'aria-hidden',
      'true',
    )
    expect(screen.getByRole('link', { name: 'Cron' })).toHaveAttribute('aria-current', 'page')
    expect(screen.queryByTestId('shell-header')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Skip to main content' })).toHaveAttribute(
      'href',
      '#main-content',
    )

    control.unmount()
    const chat = renderShellAt('/CH%61T/')

    const chatShell = document.querySelector('.shell')!
    const chatView = document.querySelector('.view-container')!
    expect(chatShell).toHaveAttribute('data-surface', 'chat')
    expect(chatShell).toHaveAttribute('data-design', 'unified')
    expect(chatView).toHaveClass('chat-surface')
    expect(chatView).toHaveClass('chat-view-enter')
    expect(chatView).not.toHaveClass('view-enter')
    expect(chatView).not.toHaveClass('control-surface')
    expect(within(chatView as HTMLElement).queryByTestId('control-header-signal')).toBeNull()
    expect(screen.getByRole('link', { name: 'Chat' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'Skip to main content' })).toHaveAttribute(
      'href',
      '#main-content',
    )
    expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content')

    chat.unmount()
    renderShellAt('/missing-page')
    expect(screen.queryByTestId('shell-header')).not.toBeInTheDocument()
    expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content')
  })

  it('focuses the current view without following the production asset base', () => {
    stubMatchMedia(false)
    const base = document.createElement('base')
    base.href = '/control/static/dist/'
    base.dataset.testSkipLink = 'true'
    document.head.prepend(base)
    const replaceState = vi.spyOn(window.history, 'replaceState')

    renderShellAt('/chat')
    const skipLink = screen.getByRole('link', { name: 'Skip to main content' })
    expect((skipLink as HTMLAnchorElement).href).toContain('/control/static/dist/#main-content')

    fireEvent.click(skipLink)

    expect(screen.getByRole('main')).toHaveFocus()
    expect(replaceState).toHaveBeenCalled()
    expect(replaceState.mock.calls.at(-1)?.[2]).not.toContain('/static/dist/')
    base.remove()
    const resetBase = document.createElement('base')
    resetBase.href = 'http://localhost:3000/'
    document.head.prepend(resetBase)
    resetBase.remove()
  })

  it('resets the persistent route scroller before entering Chat', async () => {
    stubMatchMedia(false)
    const router = createMemoryRouter([{ element: <AppShell />, children: routeChildren }], {
      initialEntries: ['/overview'],
    })
    render(
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    const main = screen.getByRole('main')
    main.scrollTop = 320
    main.scrollLeft = 12

    await act(async () => {
      await router.navigate('/chat')
    })

    expect(screen.getByRole('main')).toBe(main)
    expect(main.scrollTop).toBe(0)
    expect(main.scrollLeft).toBe(0)
    expect(main).toHaveClass('shell-main--chat')
  })

  it('restores the desktop rail preference without applying it to the mobile drawer', () => {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, 'true')

    stubMatchMedia(false)
    const desktop = renderShellAt('/sessions')
    expect(document.getElementById('sidebar-nav')).toHaveAttribute('data-collapsed', 'true')
    expect(screen.getByRole('link', { name: 'Sessions' })).toHaveAttribute('aria-current', 'page')
    desktop.unmount()

    stubMatchMedia(true)
    renderShellAt('/sessions')
    const sidebar = document.getElementById('sidebar-nav')!
    expect(sidebar).toHaveAttribute('data-collapsed', 'false')
    fireEvent.click(screen.getByRole('button', { name: 'Toggle menu' }))
    expect(screen.getByRole('link', { name: 'Sessions' })).toHaveTextContent('Sessions')
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
    expect(links).toEqual([
      'Chat',
      'Projects',
      'Overview',
      'Health',
      'Channels',
      'MCP Servers',
      'Skills',
      'Sessions',
      'Memory',
      'Agents',
      'Usage',
      'Cron',
      'Agent Setup',
      'Environment',
      'Logs',
      'Approvals',
    ])
  })

  it('keeps legacy setup and config deep links on the single Agent Setup nav item', () => {
    stubMatchMedia(false)
    const setup = renderShellAt('/setup')
    expect(screen.getByRole('link', { name: 'Agent Setup' })).toHaveAttribute(
      'aria-current',
      'page',
    )
    setup.unmount()

    renderShellAt('/config')
    expect(screen.getByRole('link', { name: 'Agent Setup' })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  it('keeps MCP navigation active while completing an OAuth callback', () => {
    stubMatchMedia(false)
    renderShellAt('/mcp/oauth/callback?error=access_denied')
    expect(screen.getByRole('link', { name: 'MCP Servers' })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  // M13 — parity: app.js:58-68 — version label 'v<semver>' derived from the
  // bootstrap version with the '+NNN' build-suffix stripped and safe-charset
  // filtered; the label is suppressed when empty while the status bar remains.
  it('renders the sidebar version footer with the build-suffix stripped', () => {
    stubMatchMedia(false)
    mockBootstrap = { ...mockBootstrap, version: '2026.7.19+1779915602' }
    renderShellAt('/cron')
    const foot = screen.getByTestId('nav-foot')
    expect(foot).toHaveTextContent('v2026.7.19')
    expect(foot).not.toHaveTextContent('1779915602')
  })

  it('suppresses only the version label when the bootstrap version is empty', () => {
    stubMatchMedia(false)
    mockBootstrap = { ...mockBootstrap, version: '' }
    renderShellAt('/cron')
    const foot = screen.getByTestId('nav-foot')
    expect(foot).toHaveTextContent('DISCONNECTED')
    expect(foot.querySelector('.shell-sidebar__version')).toBeNull()
  })

  // M14 — the sidebar footer is the shell's single persistent connection
  // indicator. The top header has been retired.
  it('shows connection state only in the sidebar footer across all states', () => {
    stubMatchMedia(false)
    renderShellAt('/cron')
    const pill = document.getElementById('conn-pill')!
    const sidebar = document.getElementById('sidebar-nav')!

    expect(screen.getAllByRole('status')).toHaveLength(1)
    expect(sidebar).toContainElement(pill)
    expect(screen.queryByTestId('shell-header')).not.toBeInTheDocument()

    // Disconnected (initial store state).
    expect(pill).toHaveTextContent('DISCONNECTED')
    expect(pill).toHaveAttribute('title', expect.stringContaining('Disconnected'))
    expect(pill).toHaveAttribute('data-variant', 'err')

    // Connecting.
    act(() => useConnection.getState().setState('connecting'))
    expect(pill).toHaveTextContent('CONNECTING')
    expect(pill).toHaveAttribute('title', expect.stringContaining('Connecting'))
    expect(pill).toHaveAttribute('data-variant', 'warn')

    // Connected stays mounted in the footer without a second readout.
    act(() => useConnection.getState().setState('connected'))
    expect(document.getElementById('conn-pill')).not.toBeNull()
    expect(pill).toHaveTextContent('CONNECTED')
    expect(pill).toHaveAttribute('title', expect.stringContaining('Connected'))
    expect(pill).toHaveAttribute('data-variant', 'ok')
  })

  it('places Chat controls in a floating route header while theme stays in the sidebar', async () => {
    stubMatchMedia(false)
    renderShellAt('/chat')

    const sidebar = document.getElementById('sidebar-nav')!
    const header = screen.getByTestId('shell-chat-header')
    const slot = screen.getByTestId('shell-chat-header-context')
    const controls = await screen.findByRole('group', { name: 'Chat session controls' })
    const theme = screen.getByRole('button', { name: /Theme: (dark|light)\. Toggle theme/ })

    expect(header).toContainElement(controls)
    expect(header).not.toHaveTextContent('Agent workspace')
    expect(sidebar).not.toContainElement(controls)
    expect(sidebar).toContainElement(theme)
    expect(slot).toContainElement(controls)
    expect(document.querySelector('.chat-stage > .chat-session-bar')).toBeNull()
    expect(document.querySelector('.shell-header')).toBeNull()
  })

  it('renders a dismissible update banner when updates.check reports outdated status', async () => {
    stubMatchMedia(false)
    useConnection.getState().setState('connected')
    mockRpcCall.mockResolvedValue({
      current: '2026.8.11',
      latest: '2026.9.9',
      status: 'outdated',
    })

    renderShellAt('/cron')

    // Wait for the banner to render
    const banner = await screen.findByTestId('update-banner')
    expect(banner).toBeInTheDocument()
    expect(
      within(banner).getByText(
        /A new version of use-agent-os is available: 2026\.8\.11 → 2026\.9\.9/i,
      ),
    ).toBeInTheDocument()

    // Dismiss the banner
    const closeBtn = within(banner).getByRole('button', { name: 'Dismiss' })
    fireEvent.click(closeBtn)

    // The banner should disappear
    await waitFor(() => expect(screen.queryByTestId('update-banner')).toBeNull())
    expect(window.localStorage.getItem(DISMISSED_VERSION_STORAGE_KEY)).toBe('2026.9.9')
  })

  it('runs the upgrade from the banner and follows the job to completion', async () => {
    stubMatchMedia(false)
    useConnection.getState().setState('connected')
    const calls: string[] = []
    let polls = 0
    mockRpcCall.mockImplementation(async (method: string) => {
      calls.push(method)
      if (method === 'updates.check') {
        return { current: '2026.8.11', latest: '2026.9.9', status: 'outdated' }
      }
      if (method === 'updates.apply') {
        return { started: true, status: 'running', logTail: ['Upgrading…'] }
      }
      if (method === 'updates.status') {
        polls += 1
        return polls < 2
          ? { status: 'running', logTail: ['Restarting managed gateway…'] }
          : { status: 'done', exitCode: 0, result: { new: '2026.9.9', verified: true } }
      }
      return {}
    })

    renderShellAt('/cron')
    const banner = await screen.findByTestId('update-banner')
    fireEvent.click(within(banner).getByRole('button', { name: 'Update now' }))

    await waitFor(() => expect(calls).toContain('updates.apply'))
    expect(calls.filter((m) => m === 'updates.apply')).toHaveLength(1)
    expect(banner).toHaveAttribute('data-state', 'running')
    // Dismiss is off while the installer runs: the job cannot be cancelled.
    expect(within(banner).getByRole('button', { name: 'Dismiss' })).toBeDisabled()

    await waitFor(() => expect(banner).toHaveAttribute('data-state', 'done'), {
      timeout: 6000,
    })
    expect(within(banner).getByText(/Updated to 2026\.9\.9/)).toBeInTheDocument()
    expect(within(banner).getByRole('button', { name: 'Reload' })).toBeInTheDocument()
  })

  it('suppresses the update banner if the version was already dismissed', async () => {
    stubMatchMedia(false)
    window.localStorage.setItem(DISMISSED_VERSION_STORAGE_KEY, '2026.9.9')
    useConnection.getState().setState('connected')
    mockRpcCall.mockResolvedValue({
      current: '2026.8.11',
      latest: '2026.9.9',
      status: 'outdated',
    })

    renderShellAt('/cron')

    // Wait for the shell to render
    await screen.findByRole('link', { name: 'Cron' })
    expect(screen.queryByTestId('update-banner')).toBeNull()
  })

  it('shows nothing when updates.check reports up-to-date or offline', async () => {
    stubMatchMedia(false)
    useConnection.getState().setState('connected')
    mockRpcCall.mockResolvedValue({
      current: '2026.8.11',
      latest: null,
      status: 'offline',
    })

    renderShellAt('/cron')

    await screen.findByRole('link', { name: 'Cron' })
    expect(screen.queryByTestId('update-banner')).toBeNull()
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

// #137 — the cheat-sheet has to be reachable without a keyboard. '?' is
// deliberately inert while focus is in an editable field (the chat composer
// autofocuses on desktop), and touch devices have no keyboard at all, so the
// shell carries a visible entry point.
describe('keyboard shortcuts entry point', () => {
  function renderShellAt(path: string) {
    const router = createMemoryRouter([{ element: <AppShell />, children: routeChildren }], {
      initialEntries: [path],
    })
    return render(
      <QueryClientProvider client={new QueryClient()}>
        <KeyboardShortcutProvider>
          <RouterProvider router={router} />
        </KeyboardShortcutProvider>
      </QueryClientProvider>,
    )
  }

  it('opens the shortcut overlay from the sidebar', async () => {
    renderShellAt('/overview')
    const button = screen.getByRole('button', { name: 'Keyboard shortcuts' })
    expect(button).toHaveAttribute('title', expect.stringContaining('?'))

    fireEvent.click(button)
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('Show this list')).toBeInTheDocument()
  })

  it('renders nothing rather than a dead button when no registry is mounted', () => {
    const router = createMemoryRouter([{ element: <AppShell />, children: routeChildren }], {
      initialEntries: ['/overview'],
    })
    render(
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    expect(screen.queryByRole('button', { name: 'Keyboard shortcuts' })).toBeNull()
  })
})

describe('navigation shortcuts in AppShell', () => {
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

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function renderShellAt(path: string) {
    const router = createMemoryRouter([{ element: <AppShell />, children: routeChildren }], {
      initialEntries: [path],
    })
    return {
      router,
      ...render(
        <QueryClientProvider client={new QueryClient()}>
          <KeyboardShortcutProvider>
            <RouterProvider router={router} />
          </KeyboardShortcutProvider>
        </QueryClientProvider>,
      ),
    }
  }

  function press(init: KeyboardEventInit) {
    return fireEvent.keyDown(document, { bubbles: true, ...init })
  }

  it('navigates to sidebar views via g-prefixed chords', async () => {
    stubMatchMedia(false)
    const { router } = renderShellAt('/overview')
    expect(router.state.location.pathname).toBe('/overview')

    press({ key: 'g', code: 'KeyG' })
    press({ key: 'c', code: 'KeyC' })
    await waitFor(() => expect(router.state.location.pathname).toBe('/chat'))

    // Wait for the composer to autofocus, then blur it so it is not focused
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message' })).toHaveFocus())
    act(() => {
      ;(document.activeElement as HTMLElement)?.blur()
    })

    press({ key: 'g', code: 'KeyG' })
    press({ key: 'h', code: 'KeyH' })
    await waitFor(() => expect(router.state.location.pathname).toBe('/health'))
  })

  it('closes the mobile drawer and focuses the main element on navigation', async () => {
    stubMatchMedia(true)
    const { router } = renderShellAt('/cron')
    const toggle = screen.getByRole('button', { name: 'Toggle menu' })

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')

    press({ key: 'g', code: 'KeyG' })
    press({ key: 'c', code: 'KeyC' })

    await waitFor(() => expect(router.state.location.pathname).toBe('/chat'))
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    await waitFor(() => expect(screen.getByRole('main')).toHaveFocus())
  })

  it('does not navigate when typing in an input/textarea', async () => {
    stubMatchMedia(false)
    const { router } = renderShellAt('/chat')
    expect(router.state.location.pathname).toBe('/chat')

    const composer = await screen.findByRole('textbox', { name: 'Message' })
    composer.focus()
    expect(composer).toHaveFocus()

    fireEvent.keyDown(composer, { bubbles: true, key: 'g', code: 'KeyG' })
    fireEvent.keyDown(composer, { bubbles: true, key: 'o', code: 'KeyO' })

    expect(router.state.location.pathname).toBe('/chat')
  })
})
