import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, Outlet, useLocation } from 'react-router'
import {
  Activity,
  BarChart3,
  Bot,
  CalendarClock,
  LayoutDashboard,
  Menu,
  MessageSquare,
  Moon,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  Puzzle,
  Radio,
  ScrollText,
  Settings2,
  ShieldCheck,
  Sun,
  History,
  type LucideIcon,
} from 'lucide-react'
import { Toaster } from '@/components/ui/sonner'
import { Button } from '@/components/ui/button'
import { ApprovalPrompt } from '@/components/ApprovalPrompt'
import { AsciiField } from '@/components/AsciiField'
import { useTheme } from '@/stores/theme'
import { useConnection } from '@/stores/connection'
import { useApprovals } from '@/services/approval-monitor'
import { useBootstrap } from './providers'
import { defaultViewPath } from './routes'
import { ShellHeaderSlotProvider } from './ShellHeaderSlot'
import agentosMark from '@/assets/agentos-mark.png'

// app.js:72-88 — legacy sidebar information architecture: nav items grouped
// under labels, Chat first, Approvals last under Settings. Order within each
// group matches the legacy markup exactly.
const NAV_GROUPS: ReadonlyArray<{
  label: string
  items: ReadonlyArray<{ path: string; title: string; icon: LucideIcon }>
}> = [
  { label: 'Chat', items: [{ path: 'chat', title: 'Chat', icon: MessageSquare }] },
  {
    label: 'Control',
    items: [
      { path: 'overview', title: 'Overview', icon: LayoutDashboard },
      { path: 'health', title: 'Health', icon: Activity },
      { path: 'channels', title: 'Channels', icon: Radio },
      { path: 'mcp', title: 'MCP Servers', icon: Network },
      { path: 'skills', title: 'Skills', icon: Puzzle },
      { path: 'sessions', title: 'Sessions', icon: History },
      { path: 'agents', title: 'Agents', icon: Bot },
      { path: 'usage', title: 'Usage', icon: BarChart3 },
      { path: 'cron', title: 'Cron', icon: CalendarClock },
    ],
  },
  {
    label: 'Settings',
    items: [
      { path: 'settings', title: 'Agent setup', icon: Settings2 },
      { path: 'logs', title: 'Logs', icon: ScrollText },
      { path: 'approvals', title: 'Approvals', icon: ShieldCheck },
    ],
  },
]

// app.js:123 — the drawer breakpoint shared with the legacy CSS.
function mobileQuery(): MediaQueryList | null {
  try {
    return window.matchMedia('(max-width: 768px)')
  } catch {
    return null
  }
}

// app.js:58-68 — the footer shows a stable semver: strip the "+NNN" cache-buster
// build-suffix and whitelist to safe semver chars (defense-in-depth against a
// tampered data attr). An absent/empty version suppresses the block entirely so
// a bare "v" never renders.
function sidebarVersion(rawVersion: string): string {
  return (rawVersion.split('+')[0] || '').replace(/[^0-9A-Za-z.\-]/g, '').slice(0, 32)
}

// app.js:174-183 — legacy maps rpc state to a persistent pill variant + label.
const PILL_VARIANT: Record<string, string> = {
  connected: 'ok',
  connecting: 'warn',
  disconnected: 'err',
}

export const SIDEBAR_COLLAPSED_STORAGE_KEY = 'agentos-sidebar-collapsed'

function storedSidebarCollapsed(): boolean {
  if (typeof window === 'undefined') return false
  try {
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === 'true'
  } catch {
    return false
  }
}

function normalizedRoutePath(pathname: string): string {
  const trimmed = pathname.replace(/^\/+|\/+$/g, '')
  try {
    return decodeURIComponent(trimmed)
      .replace(/^\/+|\/+$/g, '')
      .toLowerCase()
  } catch {
    // A malformed escape belongs to the 404 route, but must never crash the
    // shell while it decides which visual surface to mount.
    return trimmed.toLowerCase()
  }
}

export function AppShell() {
  const mode = useTheme((s) => s.mode)
  const toggle = useTheme((s) => s.toggle)
  const connState = useConnection((s) => s.state)
  // approval_monitor.js:118-138 — the pending approval count drives a nav badge
  // on the Approvals item (legacy #approval-count, hidden at 0).
  const approvalCount = useApprovals((s) => s.count)
  const bootstrap = useBootstrap()
  const location = useLocation()

  // app.js:119-171 — mobile sidebar drawer: hamburger toggle, close on
  // nav-click / outside-click / Escape, aria-expanded + aria-hidden/inert sync.
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(storedSidebarCollapsed)
  const [isMobile, setIsMobile] = useState(() => mobileQuery()?.matches ?? false)
  const [headerSlot, setHeaderSlot] = useState<HTMLDivElement | null>(null)
  const [primaryActionSlot, setPrimaryActionSlot] = useState<HTMLDivElement | null>(null)
  const sidebarRef = useRef<HTMLElement | null>(null)
  const toggleRef = useRef<HTMLButtonElement | null>(null)
  const mainRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    const mq = mobileQuery()
    if (!mq) return
    const sync = () => setIsMobile(mq.matches)
    // app.js:131-135 — modern addEventListener with addListener fallback.
    if (typeof mq.addEventListener === 'function') {
      mq.addEventListener('change', sync)
      return () => mq.removeEventListener('change', sync)
    }
    mq.addListener(sync)
    return () => mq.removeListener(sync)
  }, [])

  useEffect(() => {
    if (!isMobile || !sidebarOpen) return
    // app.js:144-151 — click outside the sidebar (and not on the toggle)
    // closes the drawer; the explicit backdrop handles direct scrim clicks.
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as Node
      if (sidebarRef.current?.contains(target) || toggleRef.current?.contains(target)) return
      setSidebarOpen(false)
    }
    // app.js:152-157 — Esc closes the drawer for keyboard users.
    const onKeydown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setSidebarOpen(false)
        toggleRef.current?.focus()
      }
    }
    document.addEventListener('click', onDocClick)
    document.addEventListener('keydown', onKeydown)
    return () => {
      document.removeEventListener('click', onDocClick)
      document.removeEventListener('keydown', onKeydown)
    }
  }, [isMobile, sidebarOpen])

  useEffect(() => {
    if (!isMobile || !sidebarOpen) return
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previousOverflow
    }
  }, [isMobile, sidebarOpen])

  // app.js:160-171 — a closed drawer on mobile is hidden from AT and inert.
  const drawerHidden = isMobile && !sidebarOpen
  // Desktop keeps a compact icon rail preference. Mobile always renders the
  // full-width labelled drawer so the stored desktop preference cannot make
  // touch navigation cryptic or cramped.
  const compactSidebar = !isMobile && sidebarCollapsed

  const toggleSidebarCollapsed = () => {
    setSidebarCollapsed((collapsed) => {
      const next = !collapsed
      try {
        window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, String(next))
      } catch {
        // Storage can be disabled; the in-memory preference still works.
      }
      return next
    })
  }

  // router.js:29-66 — at the index URL (base path, no view segment) legacy
  // renders the default view WITHOUT rewriting the URL and highlights that
  // view's nav item. A NavLink can't self-activate at the base URL, so we
  // compute the active view path ourselves (router.js:41/59-66): the current
  // pathname's leading segment, or the default view when we're at the index.
  const normalizedPath = normalizedRoutePath(location.pathname)
  const atIndex = normalizedPath === ''
  const routePath = atIndex ? defaultViewPath() : normalizedPath.split('/')[0]
  const activePath = routePath === 'setup' || routePath === 'config' ? 'settings' : routePath
  const isChat = activePath === 'chat'

  // <main> persists while its route surface changes. A Control page can leave
  // this node deeply scrolled, whereas Chat owns a separate transcript scroller.
  // Reset the route root before paint so its old offset cannot clamp or animate
  // while the Chat frame is becoming visible.
  useLayoutEffect(() => {
    const main = mainRef.current
    if (!main) return
    main.scrollTop = 0
    main.scrollLeft = 0
  }, [location.pathname])

  // The sidebar footer is the shell's single connection indicator. Keeping the
  // reactive state here avoids duplicating the same readout in the header.
  const pillState = connState
  const pillVariant = PILL_VARIANT[pillState] ?? 'err'
  const pillLabel = pillState.charAt(0).toUpperCase() + pillState.slice(1)
  const pillOk = pillVariant === 'ok'

  const version = sidebarVersion(bootstrap.version)

  return (
    <div
      className="shell flex h-dvh font-sans"
      data-surface={isChat ? 'chat' : 'control'}
      data-design="unified"
      style={{ ['--shell-header-h' as string]: '0px' }}
    >
      <a className="shell-skip-link" href="#main-content">
        Skip to main content
      </a>
      <aside
        ref={sidebarRef}
        id="sidebar-nav"
        aria-hidden={drawerHidden || undefined}
        inert={drawerHidden || undefined}
        data-collapsed={compactSidebar}
        data-drawer-open={isMobile ? sidebarOpen : undefined}
        className="shell-sidebar flex shrink-0 flex-col border border-sidebar-border bg-sidebar"
      >
        <div className="shell-sidebar__head flex h-16 shrink-0 items-center gap-2 px-3">
          <div className="shell-sidebar__brand min-w-0">
            <img className="shell-sidebar__brand-mark" src={agentosMark} alt="" />
            <span className="shell-sidebar__brand-copy">
              <span>AgentOS</span>
              <span className="text-primary">Control</span>
            </span>
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            className="shell-sidebar__collapse ml-auto shrink-0 max-md:hidden"
            title={compactSidebar ? 'Expand navigation' : 'Collapse navigation'}
            aria-label={compactSidebar ? 'Expand navigation' : 'Collapse navigation'}
            aria-controls="sidebar-nav"
            aria-expanded={!compactSidebar}
            onClick={toggleSidebarCollapsed}
          >
            {compactSidebar ? (
              <PanelLeftOpen className="size-4" />
            ) : (
              <PanelLeftClose className="size-4" />
            )}
          </Button>
        </div>
        <nav aria-label="Main" className="shell-sidebar__nav flex-1 overflow-y-auto px-2.5 py-5">
          {NAV_GROUPS.map((group) => (
            <div key={group.label} className="shell-nav-group mb-6">
              <div className="shell-nav-group__label nav-group px-2.5 pb-2">{group.label}</div>
              {group.items.map((v) => {
                // router.js:59-66 — active nav item carries .is-active styling
                // AND aria-current="page" for screen readers. Lime is reserved
                // as signal: active nav gets the left rule + lime icon/text +
                // a blinking terminal caret.
                const active = activePath === v.path
                const Icon = v.icon
                // approval_monitor.js:118-138 — pending count badge on Approvals.
                const showBadge = v.path === 'approvals' && approvalCount > 0
                return (
                  <Link
                    key={v.path}
                    to={`/${v.path}`}
                    onClick={() => setSidebarOpen(false)}
                    aria-current={active ? 'page' : undefined}
                    aria-label={compactSidebar ? v.title : undefined}
                    title={compactSidebar ? v.title : undefined}
                    className={`shell-nav-link relative flex items-center gap-3 rounded-sm px-3 py-2.5 text-[14px] lowercase transition-colors duration-150 ${
                      active
                        ? 'caret-blink bg-accent font-semibold text-primary before:absolute before:inset-y-1.5 before:left-0 before:w-[2px] before:bg-primary'
                        : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground'
                    }`}
                  >
                    <span className="shell-nav-link__icon" aria-hidden="true">
                      <Icon
                        className={`size-[18px] shrink-0 ${active ? 'text-primary' : 'text-dim'}`}
                        strokeWidth={1.6}
                      />
                    </span>
                    <span className="shell-nav-link__label">{v.title}</span>
                    {showBadge ? (
                      <span
                        id="approval-count"
                        data-testid="approval-badge"
                        className="shell-nav-link__badge t-data ml-auto inline-flex min-w-5 items-center justify-center rounded-full border border-warn/40 px-1.5 text-[10px] font-semibold text-warn"
                        aria-label={`${approvalCount} pending ${
                          approvalCount === 1 ? 'approval' : 'approvals'
                        }`}
                      >
                        {approvalCount}
                      </span>
                    ) : null}
                  </Link>
                )
              })}
            </div>
          ))}
        </nav>
        {/* One authoritative TTY status bar. The connection state is always
            present; only the optional bootstrap version label is suppressed. */}
        <div className="shell-sidebar__footer mt-auto">
          <div
            id="conn-pill"
            role="status"
            aria-live="polite"
            className="shell-sidebar__connection t-data"
            data-testid="nav-foot"
            data-variant={pillVariant}
            title={version ? `${pillLabel}, version ${version}` : pillLabel}
          >
            <span
              aria-hidden="true"
              className={`shell-sidebar__status-dot size-1.5 shrink-0 rounded-full ${
                pillOk ? 'bg-ok' : pillVariant === 'warn' ? 'bg-warn' : 'bg-danger'
              }`}
            />
            <span
              className={`shell-sidebar__status-label ${
                pillOk ? 'text-ok' : pillVariant === 'warn' ? 'text-warn' : 'text-danger'
              }`}
            >
              {pillLabel.toUpperCase()}
            </span>
            {version ? <span className="shell-sidebar__version ml-auto">v{version}</span> : null}
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            className="shell-sidebar__theme"
            onClick={toggle}
            title={`Theme: ${mode}`}
            aria-label={`Theme: ${mode}. Toggle theme`}
            aria-pressed={mode === 'dark'}
          >
            {mode === 'dark' ? <Moon className="size-4" /> : <Sun className="size-4" />}
          </Button>
        </div>
      </aside>
      {isMobile && sidebarOpen ? (
        <button
          type="button"
          className="shell-sidebar__backdrop"
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
        />
      ) : null}
      <div className="shell-workspace flex min-w-0 flex-1 flex-col">
        <Button
          ref={toggleRef}
          variant="ghost"
          size="icon"
          className="shell-mobile-menu"
          title="Toggle menu"
          aria-label="Toggle menu"
          aria-controls="sidebar-nav"
          aria-expanded={sidebarOpen}
          onClick={() => setSidebarOpen((open) => !open)}
        >
          <Menu className="size-4" />
        </Button>
        {isChat ? (
          <section
            className="shell-chat-header"
            aria-label="Chat toolbar"
            data-testid="shell-chat-header"
          >
            <div className="shell-chat-header__identity">
              <span className="shell-chat-header__icon" aria-hidden="true">
                <MessageSquare />
              </span>
              <span className="shell-chat-header__copy">
                <strong>Chat</strong>
              </span>
            </div>
            <div
              ref={setPrimaryActionSlot}
              className="shell-chat-header__primary-action"
              data-testid="shell-chat-header-primary-action"
            />
            <div
              ref={setHeaderSlot}
              className="shell-chat-header__context"
              data-chat-session-context="true"
              data-testid="shell-chat-header-context"
            />
          </section>
        ) : null}
        <main
          ref={mainRef}
          id="main-content"
          tabIndex={-1}
          className={`min-h-0 flex-1 ${isChat ? 'shell-main--chat overflow-hidden' : 'shell-main--control overflow-auto'}`}
        >
          {/* Common container: every view fills and centers identically.
              Control pages use the shared whole-view entrance. Chat keeps its
              large scroll layer stationary and coordinates only its lightweight
              header/composer surfaces in chat-unified.css. */}
          <ShellHeaderSlotProvider
            target={headerSlot}
            primaryActionTarget={primaryActionSlot}
            onPrimaryAction={() => setSidebarOpen(false)}
          >
            <div
              key={location.pathname}
              className={`view-container ${
                isChat ? 'chat-surface chat-view-enter' : 'control-surface view-enter'
              }`}
            >
              {!isChat ? <AsciiField /> : null}
              <Outlet />
            </div>
          </ShellHeaderSlotProvider>
        </main>
      </div>
      {/* approval_monitor.js:140-184 — global approval prompt, mounted once. */}
      <ApprovalPrompt />
      <Toaster />
    </div>
  )
}
