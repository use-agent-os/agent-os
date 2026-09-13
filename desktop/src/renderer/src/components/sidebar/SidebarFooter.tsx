import { Bot, Home, MoreHorizontal, PenSquare, RefreshCw } from 'lucide-react'
import { NavLink } from 'react-router'
import type { GatewayState } from '@shared/gateway'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { cn } from '~/lib/utils'
import { useGateway } from '~/stores/gateway'

const LIGHT: Record<GatewayState, string> = {
  stopped: 'text-dim',
  starting: 'text-warn',
  running: 'text-ok',
  stopping: 'text-warn',
  error: 'text-danger',
}

/**
 * Icon strip at the bottom of the sidebar. The gateway light lives here as a
 * button: one glance for state, one click to start or stop.
 */
export function SidebarFooter() {
  const { status, busy, start, stop } = useGateway()
  const running = status.state === 'running' || status.state === 'starting'
  const pulsing = status.state === 'starting' || status.state === 'stopping'
  const label = running ? t('gateway.stop') : t('gateway.start')

  return (
    <div className="app-no-drag flex items-center gap-0.5 border-t border-hairline px-2 py-1.5">
      <NavLink to="/sessions" end aria-label={t('sidebar.home')} title={t('sidebar.home')}>
        {({ isActive }) => (
          <span
            className={cn(
              'mac-button flex',
              isActive ? 'text-foreground' : 'text-muted-foreground',
            )}
            data-variant={isActive ? 'secondary' : 'ghost'}
            data-size="icon"
          >
            <Home className="size-4" strokeWidth={1.75} aria-hidden />
          </span>
        )}
      </NavLink>
      <Button variant="ghost" size="icon" aria-label={t('sidebar.new')} title={t('sidebar.new')}>
        <PenSquare className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
      </Button>
      <Button variant="ghost" size="icon" aria-label={t('sidebar.sync')} title={t('sidebar.sync')}>
        <RefreshCw className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
      </Button>
      <div className="flex-1" />
      <Button
        variant="ghost"
        size="icon"
        disabled={busy}
        aria-label={label}
        title={`${t(`gateway.state.${status.state}`)}${status.url ? ` · ${status.url}` : ''}${status.error ? ` · ${status.error}` : ''}`}
        onClick={() => void (running ? stop() : start())}
      >
        <span className="relative flex">
          <Bot className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
          <span
            className={cn('mac-light absolute -top-0.5 -right-1 size-1.5', LIGHT[status.state])}
            data-pulse={pulsing}
            aria-hidden
          />
        </span>
      </Button>
      <Button variant="ghost" size="icon" aria-label={t('sidebar.more')} title={t('sidebar.more')}>
        <MoreHorizontal className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
      </Button>
    </div>
  )
}
