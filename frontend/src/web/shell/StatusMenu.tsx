import { ArrowUpCircle, Keyboard, Monitor, Moon, Settings, Sun } from 'lucide-react'
import { useNavigate } from 'react-router'
import { useRpc } from '@/app/providers'
import { useShortcutOverlay } from '@/components/KeyboardShortcuts'
import { t } from '@/i18n'
import '@/i18n/en/webConnection'
import '@/i18n/en/webShell'
import {
  truthActionLabel,
  truthFreshnessLabel,
  truthReasonLabel,
  truthStateLabel,
  truthTone,
  type TruthAction,
} from '@/lib/runtime-truth'
import { cn } from '@/lib/utils'
import { useTheme, type ThemePreference } from '@/stores/theme'
import { Button } from '@/web/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'
import { SimpleTooltip } from '@/web/components/ui/tooltip'
import { useUpdateCheck } from './updates'
import { useDisplayedTruth } from './use-displayed-connection'

const TONE_CLASS = {
  ok: 'text-ok',
  warn: 'text-warn',
  danger: 'text-danger',
  dim: 'text-text-muted',
} as const

/**
 * The rail's status/account menu (connection spec "Connection status
 * indicator"): the runtime truth state and its freshness, the reason, exactly
 * one recovery action, plus theme, shortcuts, settings and update status.
 */
export function StatusMenu() {
  const truth = useDisplayedTruth()
  const rpc = useRpc()
  const navigate = useNavigate()
  const preference = useTheme((s) => s.preference)
  const setPreference = useTheme((s) => s.setPreference)
  const overview = useShortcutOverlay()
  const update = useUpdateCheck()
  const state = truthStateLabel(truth.state)
  const freshness = truthFreshnessLabel(truth)
  const label = `${t('webShell.railStatus')}: ${state}`
  const tone = truthTone(truth.state)

  const recover = (action: TruthAction) => {
    if (action === 'retry_connection' || action === 'start_gateway') rpc.reconnect({ force: true })
    else if (action === 'open_inbox') navigate('/inbox')
    else navigate('/settings/health')
  }

  return (
    <DropdownMenu>
      <SimpleTooltip label={`${state} · ${freshness}`} side="right">
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            aria-label={label}
            data-truth-state={truth.state}
            className="relative hover:bg-surface-2 data-[state=open]:bg-surface-3"
          >
            <span
              aria-hidden
              className={cn(
                'grid size-5 place-items-center rounded-full bg-current/15',
                TONE_CLASS[tone],
              )}
            >
              <span
                data-status-icon
                data-live={tone === 'warn' || undefined}
                className={cn('status-dot size-2', tone === 'warn' && 'motion-status')}
              />
            </span>
            {update.available ? (
              <span
                aria-hidden
                className="absolute top-1 right-1 size-2 rounded-full bg-info ring-2 ring-surface-1"
              />
            ) : null}
          </Button>
        </DropdownMenuTrigger>
      </SimpleTooltip>
      <DropdownMenuContent side="right" align="end" sideOffset={8} className="w-72">
        <div
          className="mb-1 flex gap-2.5 rounded-md border border-border bg-surface-2 px-2.5 py-2 shadow-highlight"
          data-testid="truth-summary"
        >
          <span aria-hidden className={cn('mt-1.5 flex', TONE_CLASS[tone])}>
            <span className="status-dot size-2" />
          </span>
          <div className="min-w-0 flex-1">
            <p className="font-semibold">{state}</p>
            <p className="text-small text-text-muted">{freshness}</p>
            <p className="mt-1 text-small text-pretty">{truthReasonLabel(truth)}</p>
          </div>
        </div>
        <DropdownMenuItem onSelect={() => recover(truth.action)}>
          {truthActionLabel(truth.action)}
        </DropdownMenuItem>
        {update.available ? (
          <DropdownMenuItem onSelect={() => navigate('/settings/updates')}>
            <ArrowUpCircle aria-hidden />
            {t('webConnection.updateAvailable')}
          </DropdownMenuItem>
        ) : null}
        <DropdownMenuSeparator />
        <DropdownMenuLabel>{t('webShell.themeLabel')}</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={preference}
          onValueChange={(v) => setPreference(v as ThemePreference)}
        >
          <DropdownMenuRadioItem value="system">
            <Monitor aria-hidden />
            {t('webShell.themeSystem')}
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="light">
            <Sun aria-hidden />
            {t('webShell.themeLight')}
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="dark">
            <Moon aria-hidden />
            {t('webShell.themeDark')}
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => overview.open()}>
          <Keyboard aria-hidden />
          {t('webShell.shortcutsOpen')}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => navigate('/settings')}>
          <Settings aria-hidden />
          {t('webShell.openSettings')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
