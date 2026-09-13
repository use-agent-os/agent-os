import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react'
import { Link, Outlet, useLocation, useNavigate } from 'react-router'
import {
  Activity,
  BarChart3,
  Bot,
  Brain,
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  FolderKanban,
  LayoutDashboard,
  Menu,
  MessageSquare,
  Moon,
  Network,
  KeyRound,
  Keyboard,
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
import { AsciiField } from '@/components/AsciiField'
import {
  formatCombo,
  HELP_COMBO,
  useKeyboardShortcut,
  useShortcutOverlay,
} from '@/components/KeyboardShortcuts'
import { t, tPlural, useLocale, type MessageKey } from '@/i18n'
import { useTheme } from '@/stores/theme'
import { useConnection } from '@/stores/connection'
import { useApprovals } from '@/services/approval-monitor'
import { useBootstrap, useRpc } from './providers'
import { defaultViewPath } from './routes'
import { ShellHeaderSlotProvider } from './ShellHeaderSlot'
import { UpdateBanner, type UpdateCheck } from './UpdateBanner'
import agentosMark from '@/assets/agentos-mark.png'

const ApprovalPrompt = lazy(async () => ({
  default: (await import('@/components/ApprovalPrompt')).ApprovalPrompt,
}))

// app.js:72-88 — legacy sidebar information architecture: nav items grouped
// under labels, Chat first, Approvals last under Settings. Order within each
// group matches the legacy markup exactly. Built per render rather than held in
// a module constant: labels resolved at module-evaluation time would freeze and
// keep the boot locale forever (#258).
function navGroups(): ReadonlyArray<{
  label: string
  items: ReadonlyArray<{ path: string; title: string; icon: LucideIcon }>
}> {
  return [
    {
      label: t('shell.navGroupChat'),
      items: [
        { path: 'chat', title: t('shell.viewChat'), icon: MessageSquare },
        { path: 'projects', title: t('shell.viewProjects'), icon: FolderKanban },
      ],
    },
    {
      label: t('shell.navGroupControl'),
      items: [
        { path: 'overview', title: t('shell.viewOverview'), icon: LayoutDashboard },
        { path: 'health', title: t('shell.viewHealth'), icon: Activity },
        { path: 'channels', title: t('shell.viewChannels'), icon: Radio },
        { path: 'mcp', title: t('shell.viewMcp'), icon: Network },
        { path: 'skills', title: t('shell.viewSkills'), icon: Puzzle },
        { path: 'sessions', title: t('shell.viewSessions'), icon: History },
        { path: 'memory', title: t('shell.viewMemory'), icon: Brain },
        { path: 'agents', title: t('shell.viewAgents'), icon: Bot },
        { path: 'usage', title: t('shell.viewUsage'), icon: BarChart3 },
        { path: 'cron', title: t('shell.viewCron'), icon: CalendarClock },
      ],
    },
    {
      label: t('shell.navGroupSettings'),
      items: [
        { path: 'settings', title: t('shell.viewSettings'), icon: Settings2 },
        { path: 'env', title: t('shell.viewEnv'), icon: KeyRound },
        { path: 'logs', title: t('shell.viewLogs'), icon: ScrollText },
        { path: 'approvals', title: t('shell.viewApprovals'), icon: ShieldCheck },
      ],
    },
  ]
}

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

// Legacy derived the label by capitalising the state id. Translating it needs a
// real map; an unmapped state still falls back to the capitalised id so a new
// RpcState can never render an empty pill.
function pillLabel(state: string): string {
  const labels: Record<string, string> = {
    connected: t('shell.connConnected'),
    connecting: t('shell.connConnecting'),
    disconnected: t('shell.connDisconnected'),
  }
  return labels[state] ?? state.charAt(0).toUpperCase() + state.slice(1)
}

export const SIDEBAR_COLLAPSED_STORAGE_KEY = 'agentos-sidebar-collapsed'
export const DISMISSED_VERSION_STORAGE_KEY = 'agentos.dismissedVersion'

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

export interface NavShortcutSpec {
  combo: string
  path: string
  descriptionKey: MessageKey
}

export const NAV_SHORTCUTS: ReadonlyArray<NavShortcutSpec> = [
  { combo: 'g c', path: 'chat', descriptionKey: 'shell.shortcutNavChat' },
  { combo: 'g o', path: 'overview', descriptionKey: 'shell.shortcutNavOverview' },
  { combo: 'g h', path: 'health', descriptionKey: 'shell.shortcutNavHealth' },
  { combo: 'g n', path: 'channels', descriptionKey: 'shell.shortcutNavChannels' },
  { combo: 'g m', path: 'mcp', descriptionKey: 'shell.shortcutNavMcp' },
  { combo: 'g k', path: 'skills', descriptionKey: 'shell.shortcutNavSkills' },
  { combo: 'g j', path: 'projects', descriptionKey: 'shell.shortcutNavProjects' },
  { combo: 'g s', path: 'sessions', descriptionKey: 'shell.shortcutNavSessions' },
  { combo: 'g y', path: 'memory', descriptionKey: 'shell.shortcutNavMemory' },
  { combo: 'g a', path: 'agents', descriptionKey: 'shell.shortcutNavAgents' },
  { combo: 'g u', path: 'usage', descriptionKey: 'shell.shortcutNavUsage' },
  { combo: 'g r', path: 'cron', descriptionKey: 'shell.shortcutNavCron' },
  { combo: 'g ,', path: 'settings', descriptionKey: 'shell.shortcutNavSettings' },
  { combo: 'g e', path: 'env', descriptionKey: 'shell.shortcutNavEnv' },
  { combo: 'g l', path: 'logs', descriptionKey: 'shell.shortcutNavLogs' },
  { combo: 'g p', path: 'approvals', descriptionKey: 'shell.shortcutNavApprovals' },
]

function NavShortcut({
  combo,
  descriptionKey,
  path,
  navigateTo,
}: {
  combo: string
  descriptionKey: MessageKey
  path: string
  navigateTo: (path: string) => void
}) {
  useKeyboardShortcut(
    {
      combo,
      description: t(descriptionKey),
      category: t('shell.shortcutCategoryNavigation'),
    },
    (e) => {
      e.preventDefault()
      navigateTo(path)
    },
  )
  return null
}

export function AppShell() {
  // #258 — the shell is the one subscriber the whole console needs. Its own
  // chrome re-renders here; the routed view is remounted through the container
  // key below, because `<Outlet/>` hands back the same element object on a
  // parent re-render and React would otherwise bail out of the subtree.
  const locale = useLocale()
  const mode = useTheme((s) => s.mode)
  const toggle = useTheme((s) => s.toggle)
  const connState = useConnection((s) => s.state)
  // approval_monitor.js:118-138 — the pending approval count drives a nav badge
  // on the Approvals item (legacy #approval-count, hidden at 0).
  const approvalCount = useApprovals((s) => s.count)
  const hasPendingApprovals = useApprovals((s) => s.pending.length > 0)
  const bootstrap = useBootstrap()
  const location = useLocation()
  const shortcutOverlay = useShortcutOverlay()
  const navigate = useNavigate()

  const rpc = useRpc()
  const [updateStatus, setUpdateStatus] = useState<UpdateCheck | null>(null)
  const [dismissedVersion, setDismissedVersion] = useState<string | null>(() => {
    try {
      return localStorage.getItem(DISMISSED_VERSION_STORAGE_KEY)
    } catch {
      return null
    }
  })

  useEffect(() => {
    if (connState !== 'connected') return
    let active = true
    rpc
      .call<UpdateCheck>('updates.check')
      .then((res) => {
        if (active && res) {
          setUpdateStatus(res)
        }
      })
      .catch(() => {})
    return () => {
      active = false
    }
  }, [rpc, connState])

  const handleDismiss = () => {
    if (updateStatus?.latest) {
      try {
        localStorage.setItem(DISMISSED_VERSION_STORAGE_KEY, updateStatus.latest)
      } catch {
        /* ignore */
      }
      setDismissedVersion(updateStatus.latest)
    }
  }

  // Documented, not dispatched: the drawer binds Escape itself (below) because
  // it has to run while focus is inside the drawer, which the global editable
  // guard would otherwise skip.
  useKeyboardShortcut(
    {
      combo: 'escape',
      description: t('shell.shortcutDrawerEscape'),
      category: t('shell.shortcutCategoryGlobal'),
      documentationOnly: true,
    },
    () => {},
  )

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
  const restoreDrawerFocusRef = useRef(false)

  const closeMobileDrawer = useCallback((restoreTriggerFocus: boolean) => {
    restoreDrawerFocusRef.current = restoreTriggerFocus
    setSidebarOpen(false)
  }, [])

  const navigateTo = useCallback(
    (path: string) => {
      navigate(`/${path}`)
      closeMobileDrawer(false)
      setTimeout(() => {
        mainRef.current?.focus({ preventScroll: true })
      }, 0)
    },
    [navigate, closeMobileDrawer],
  )

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
      closeMobileDrawer(true)
    }
    // app.js:152-157 — Esc closes the drawer for keyboard users.
    const onKeydown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        closeMobileDrawer(true)
        return
      }
      if (e.key !== 'Tab') return

      const sidebar = sidebarRef.current
      if (!sidebar) return
      const focusable = Array.from(
        sidebar.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]):not([tabindex="-1"]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute('inert') && !element.closest('[inert]'))
      if (focusable.length === 0) {
        e.preventDefault()
        sidebar.focus({ preventScroll: true })
        return
      }

      const first = focusable[0]!
      const last = focusable.at(-1)!
      const active = document.activeElement
      if (e.shiftKey && (active === first || !sidebar.contains(active))) {
        e.preventDefault()
        last.focus({ preventScroll: true })
      } else if (!e.shiftKey && (active === last || !sidebar.contains(active))) {
        e.preventDefault()
        first.focus({ preventScroll: true })
      }
    }
    document.addEventListener('click', onDocClick)
    document.addEventListener('keydown', onKeydown)
    return () => {
      document.removeEventListener('click', onDocClick)
      document.removeEventListener('keydown', onKeydown)
    }
  }, [closeMobileDrawer, isMobile, sidebarOpen])

  useLayoutEffect(() => {
    if (!isMobile) {
      restoreDrawerFocusRef.current = false
      return
    }
    if (sidebarOpen) {
      restoreDrawerFocusRef.current = false
      const sidebar = sidebarRef.current
      const target =
        sidebar?.querySelector<HTMLElement>('[aria-current="page"]') ??
        sidebar?.querySelector<HTMLElement>('a[href], button:not([disabled])')
      target?.focus({ preventScroll: true })
      return
    }
    // Restore only after the closing commit has removed `inert` from the
    // workspace. Approval-driven closes deliberately leave focus to the
    // critical ApprovalPrompt that mounts in the same commit.
    if (restoreDrawerFocusRef.current) {
      restoreDrawerFocusRef.current = false
      toggleRef.current?.focus({ preventScroll: true })
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

  useEffect(
    () =>
      useApprovals.subscribe((state) => {
        if (state.pending.length > 0) closeMobileDrawer(false)
      }),
    [closeMobileDrawer],
  )

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
  const connectionLabel = pillLabel(pillState)
  const pillOk = pillVariant === 'ok'

  const version = sidebarVersion(bootstrap.version)
  const themeName = mode === 'dark' ? t('shell.themeDark') : t('shell.themeLight')

  const focusMainContent = (event: ReactMouseEvent<HTMLAnchorElement>) => {
    event.preventDefault()
    const main = mainRef.current
    if (!main) return
    main.focus({ preventScroll: true })
    try {
      const current = new URL(window.location.href)
      current.hash = 'main-content'
      window.history.replaceState(
        window.history.state,
        '',
        `${current.pathname}${current.search}${current.hash}`,
      )
    } catch {
      // Focus is the accessibility behavior; the cosmetic hash is optional.
    }
  }

  return (
    <div
      className="shell flex h-dvh font-sans"
      data-surface={isChat ? 'chat' : 'control'}
      data-design="unified"
      style={{ ['--shell-header-h' as string]: '0px' }}
    >
      {NAV_SHORTCUTS.map((ns) => (
        <NavShortcut
          key={ns.combo}
          combo={ns.combo}
          descriptionKey={ns.descriptionKey}
          path={ns.path}
          navigateTo={navigateTo}
        />
      ))}
      <a className="shell-skip-link" href="#main-content" onClick={focusMainContent}>
        {t('shell.navSkipToContent')}
      </a>
      <aside
        ref={sidebarRef}
        id="sidebar-nav"
        role={isMobile && sidebarOpen ? 'dialog' : undefined}
        aria-label={isMobile && sidebarOpen ? t('shell.navDrawerLandmark') : undefined}
        aria-modal={isMobile && sidebarOpen ? true : undefined}
        aria-hidden={drawerHidden || undefined}
        inert={drawerHidden || undefined}
        tabIndex={isMobile && sidebarOpen ? -1 : undefined}
        data-collapsed={compactSidebar}
        data-drawer-open={isMobile ? sidebarOpen : undefined}
        className="shell-sidebar flex shrink-0 flex-col border border-sidebar-border bg-sidebar"
      >
        <div className="shell-sidebar__head flex h-16 shrink-0 items-center gap-2 px-3">
          <div className="shell-sidebar__brand min-w-0">
            <img className="shell-sidebar__brand-mark" src={agentosMark} alt="" />
            <span className="shell-sidebar__brand-copy">
              <span>{t('shell.brandName')}</span>
              <span className="text-primary">{t('shell.brandSuffix')}</span>
            </span>
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            className="shell-sidebar__collapse ml-auto shrink-0 max-md:hidden"
            title={compactSidebar ? t('shell.navExpand') : t('shell.navCollapse')}
            aria-label={compactSidebar ? t('shell.navExpand') : t('shell.navCollapse')}
            aria-controls="sidebar-nav"
            aria-expanded={!compactSidebar}
            aria-hidden={isMobile || undefined}
            tabIndex={isMobile ? -1 : undefined}
            onClick={toggleSidebarCollapsed}
          >
            {compactSidebar ? (
              <ChevronRight className="size-4" />
            ) : (
              <ChevronLeft className="size-4" />
            )}
          </Button>
        </div>
        <nav
          aria-label={t('shell.navLandmark')}
          className="shell-sidebar__nav flex-1 overflow-y-auto px-2.5 py-5"
        >
          {navGroups().map((group) => (
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
                    onClick={() => closeMobileDrawer(false)}
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
                        aria-label={tPlural('shell.navApprovalBadge', approvalCount)}
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
            title={
              version
                ? t('shell.connWithVersion', { state: connectionLabel, version })
                : connectionLabel
            }
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
              {connectionLabel.toUpperCase()}
            </span>
            {version ? <span className="shell-sidebar__version ml-auto">v{version}</span> : null}
          </div>
          {/* #137: the overlay needs a way in that is not a keyboard shortcut.
              '?' is deliberately inert while focus sits in the composer (which
              autofocuses on desktop) and on touch devices there is no keyboard
              at all, so a discoverability feature reachable only by key would
              not be discoverable. */}
          {shortcutOverlay.available ? (
            <Button
              variant="ghost"
              size="icon-sm"
              className="shell-sidebar__shortcuts"
              onClick={shortcutOverlay.open}
              title={t('shell.shortcutsTitle', { combo: formatCombo(HELP_COMBO) })}
              aria-label={t('shell.shortcutsLabel')}
            >
              <Keyboard className="size-4" />
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="icon-sm"
            className="shell-sidebar__theme"
            onClick={toggle}
            title={t('shell.themeTitle', { mode: themeName })}
            aria-label={t('shell.themeToggleLabel', { mode: themeName })}
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
          aria-label={t('shell.navClose')}
          onClick={() => closeMobileDrawer(true)}
        />
      ) : null}
      <div
        className="shell-workspace flex min-w-0 flex-1 flex-col"
        inert={(isMobile && sidebarOpen) || undefined}
        aria-hidden={(isMobile && sidebarOpen) || undefined}
      >
        <Button
          ref={toggleRef}
          variant="ghost"
          size="icon"
          className="shell-mobile-menu"
          title={t('shell.navToggleMenu')}
          aria-label={t('shell.navToggleMenu')}
          aria-controls="sidebar-nav"
          aria-expanded={sidebarOpen}
          onClick={() => {
            if (sidebarOpen) {
              closeMobileDrawer(true)
            } else {
              restoreDrawerFocusRef.current = false
              setSidebarOpen(true)
            }
          }}
        >
          <Menu className="size-4" />
        </Button>
        {updateStatus &&
        updateStatus.status === 'outdated' &&
        updateStatus.latest !== dismissedVersion ? (
          <UpdateBanner check={updateStatus} onDismiss={handleDismiss} />
        ) : null}
        {isChat ? (
          <section
            className="shell-chat-header"
            aria-label={t('shell.chatToolbar')}
            data-testid="shell-chat-header"
          >
            <div className="shell-chat-header__identity">
              <span className="shell-chat-header__icon" aria-hidden="true">
                <MessageSquare />
              </span>
              <span className="shell-chat-header__copy">
                <strong>{t('shell.viewChat')}</strong>
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
              key={`${locale}:${location.pathname}`}
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
      {/* approval_monitor.js:140-184 — global approval prompt, loaded only when needed. */}
      {hasPendingApprovals ? (
        <Suspense fallback={null}>
          <ApprovalPrompt />
        </Suspense>
      ) : null}
      <Toaster />
    </div>
  )
}
