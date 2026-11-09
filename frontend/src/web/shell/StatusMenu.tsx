import { Circle, Keyboard, Monitor, Moon, Settings, Sun } from 'lucide-react'
import { useNavigate } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { cn } from '@/lib/utils'
import { useConnection } from '@/stores/connection'
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
import { useShell } from './shell-store'

export function connectionLabel(state: string): string {
  if (state === 'connected') return t('webShell.statusConnected')
  if (state === 'connecting') return t('webShell.statusConnecting')
  return t('webShell.statusOffline')
}

/** The rail's status/account menu: connection state, theme, settings. */
export function StatusMenu() {
  const state = useConnection((s) => s.state)
  const preference = useTheme((s) => s.preference)
  const setPreference = useTheme((s) => s.setPreference)
  const navigate = useNavigate()
  const openShortcuts = useShell((s) => s.setShortcutsOpen)
  const label = `${t('webShell.railStatus')}: ${connectionLabel(state)}`

  return (
    <DropdownMenu>
      <SimpleTooltip label={label} side="right">
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon" aria-label={label} className="relative">
            <Circle
              aria-hidden
              data-status-icon
              className={cn(
                'size-3 fill-current',
                state === 'connected'
                  ? 'text-ok'
                  : state === 'connecting'
                    ? 'text-warn'
                    : 'text-danger',
              )}
            />
          </Button>
        </DropdownMenuTrigger>
      </SimpleTooltip>
      <DropdownMenuContent side="right" align="end">
        <DropdownMenuLabel>{connectionLabel(state)}</DropdownMenuLabel>
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
        <DropdownMenuItem onSelect={() => openShortcuts(true)}>
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
