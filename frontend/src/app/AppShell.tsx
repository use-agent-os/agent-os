import { useEffect, useRef, useState } from 'react'
import { Link, Outlet, useLocation } from 'react-router'
import { Menu, Moon, Sun } from 'lucide-react'
import { Toaster } from '@/components/ui/sonner'
import { Button } from '@/components/ui/button'
import { useTheme } from '@/stores/theme'
import { useConnection } from '@/stores/connection'
import { useBootstrap } from './providers'
import { defaultViewPath } from './routes'

// app.js:72-88 — legacy sidebar information architecture: nav items grouped
// under labels, Chat first, Approvals last under Settings. Order within each
// group matches the legacy markup exactly.
const NAV_GROUPS: ReadonlyArray<{
  label: string
  items: ReadonlyArray<{ path: string; title: string }>
}> = [
  { label: 'Chat', items: [{ path: 'chat', title: 'Chat' }] },
  {
    label: 'Control',
    items: [
      { path: 'overview', title: 'Overview' },
      { path: 'health', title: 'Health' },
      { path: 'channels', title: 'Channels' },
      { path: 'skills', title: 'Skills' },
      { path: 'sessions', title: 'Sessions' },
      { path: 'agents', title: 'Agents' },
      { path: 'usage', title: 'Usage' },
      { path: 'cron', title: 'Cron' },
    ],
  },
  {
    label: 'Settings',
    items: [
      { path: 'setup', title: 'Setup' },
      { path: 'config', title: 'Config' },
      { path: 'logs', title: 'Logs' },
      { path: 'approvals', title: 'Approvals' },
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

export function AppShell() {
  const mode = useTheme((s) => s.mode)
  const toggle = useTheme((s) => s.toggle)
  const connState = useConnection((s) => s.state)
  const bootstrap = useBootstrap()
  const location = useLocation()

  // app.js:119-171 — mobile sidebar drawer: hamburger toggle, close on
  // nav-click / outside-click / Escape, aria-expanded + aria-hidden/inert sync.
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [isMobile, setIsMobile] = useState(() => mobileQuery()?.matches ?? false)
  const sidebarRef = useRef<HTMLElement | null>(null)
  const toggleRef = useRef<HTMLButtonElement | null>(null)

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
    if (!sidebarOpen) return
    // app.js:144-151 — click outside the sidebar (and not on the toggle)
    // closes the drawer; the CSS backdrop can't receive pointer events.
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as Node
      if (sidebarRef.current?.contains(target) || toggleRef.current?.contains(target)) return
      setSidebarOpen(false)
    }
    // app.js:152-157 — Esc closes the drawer for keyboard users.
    const onKeydown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSidebarOpen(false)
    }
    document.addEventListener('click', onDocClick)
    document.addEventListener('keydown', onKeydown)
    return () => {
      document.removeEventListener('click', onDocClick)
      document.removeEventListener('keydown', onKeydown)
    }
  }, [sidebarOpen])

  // app.js:160-171 — a closed drawer on mobile is hidden from AT and inert.
  const drawerHidden = isMobile && !sidebarOpen

  // router.js:29-66 — at the index URL (base path, no view segment) legacy
  // renders the default view WITHOUT rewriting the URL and highlights that
  // view's nav item. A NavLink can't self-activate at the base URL, so we
  // compute the active view path ourselves (router.js:41/59-66): the current
  // pathname's leading segment, or the default view when we're at the index.
  const atIndex = location.pathname === '/' || location.pathname === ''
  const activePath = atIndex ? defaultViewPath() : location.pathname.replace(/^\//, '')

  // app.js:174-183 — persistent connection pill: never unmounts, shows a compact
  // "Connected" ok state with a title attr. Label is the capitalized state.
  const pillState = connState
  const pillVariant = PILL_VARIANT[pillState] ?? 'err'
  const pillLabel = pillState.charAt(0).toUpperCase() + pillState.slice(1)
  const pillOk = pillVariant === 'ok'

  const version = sidebarVersion(bootstrap.version)

  return (
    <div className="flex h-dvh font-sans">
      <aside
        ref={sidebarRef}
        id="sidebar-nav"
        aria-hidden={drawerHidden || undefined}
        inert={drawerHidden || undefined}
        className={`flex w-60 shrink-0 flex-col border-r border-sidebar-border bg-sidebar max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-40 max-md:transition-transform ${
          sidebarOpen ? '' : 'max-md:-translate-x-full'
        }`}
      >
        {/* Wordmark: tight heavy sans, the lime mark dot is the only signal use here. */}
        <div className="flex items-baseline gap-1.5 border-b border-hairline px-4 py-4">
          <span className="text-[15px] font-extrabold tracking-tight">AgentOS</span>
          <span className="t-label !text-primary">Control</span>
        </div>
        <nav aria-label="Main" className="flex-1 overflow-y-auto px-2 py-3">
          {NAV_GROUPS.map((group) => (
            <div key={group.label} className="mb-4">
              <div className="t-label px-2 pb-1.5">{group.label}</div>
              {group.items.map((v) => {
                // router.js:59-66 — active nav item carries .is-active styling
                // AND aria-current="page" for screen readers. Lime is reserved
                // as signal: active nav gets the left rule + lime text.
                const active = activePath === v.path
                return (
                  <Link
                    key={v.path}
                    to={`/${v.path}`}
                    onClick={() => setSidebarOpen(false)}
                    aria-current={active ? 'page' : undefined}
                    className={`relative block rounded-sm px-3 py-1.5 text-sm transition-colors duration-150 ${
                      active
                        ? 'bg-accent font-semibold text-primary before:absolute before:inset-y-1 before:left-0 before:w-[2px] before:bg-primary'
                        : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground'
                    }`}
                  >
                    {v.title}
                  </Link>
                )
              })}
            </div>
          ))}
        </nav>
        {/* app.js:66-68,88 — version footer, suppressed when version is empty. */}
        {version && (
          <div
            className="t-data mt-auto border-t border-hairline px-4 py-3 text-[11px] text-dim"
            data-testid="nav-foot"
          >
            v{version}
          </div>
        )}
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center justify-between border-b border-hairline bg-surface/60 px-4">
          <div className="flex items-center gap-3">
            <Button
              ref={toggleRef}
              variant="ghost"
              size="icon"
              className="md:hidden"
              title="Toggle menu"
              aria-label="Toggle menu"
              aria-controls="sidebar-nav"
              aria-expanded={sidebarOpen}
              onClick={() => setSidebarOpen((open) => !open)}
            >
              <Menu className="size-4" />
            </Button>
            {/* app.js:94,174-183 — persistent connection pill; never unmounts.
                Tactical readout: mono uppercase, square, status dot carries the
                semantic state (the one permitted status dot on this surface). */}
            <span
              id="conn-pill"
              role="status"
              aria-live="polite"
              title={pillLabel}
              data-variant={pillVariant}
              className={`t-data inline-flex items-center gap-1.5 rounded-sm border px-2 py-0.5 text-[11px] uppercase tracking-[0.14em] ${
                pillOk
                  ? 'border-ok/30 text-ok'
                  : pillVariant === 'warn'
                    ? 'border-warn/30 text-warn'
                    : 'border-danger/30 text-danger'
              }`}
            >
              <span
                aria-hidden="true"
                className={`size-1.5 rounded-full ${
                  pillOk ? 'bg-ok' : pillVariant === 'warn' ? 'bg-warn' : 'bg-danger'
                }`}
              />
              {pillLabel}
            </span>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="ml-auto"
            onClick={toggle}
            title={`Theme: ${mode}`}
            aria-label={`Theme: ${mode}. Toggle theme`}
            aria-pressed={mode === 'dark'}
          >
            {mode === 'dark' ? <Moon className="size-4" /> : <Sun className="size-4" />}
          </Button>
        </header>
        <main className="min-h-0 flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
      <Toaster />
    </div>
  )
}
