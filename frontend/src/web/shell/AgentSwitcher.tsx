import { Check, Settings2 } from 'lucide-react'
import { useNavigate } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { Button } from '@/web/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'
import { SimpleTooltip } from '@/web/components/ui/tooltip'
import { useAgents } from './session-data'
import { useShell } from './shell-store'

function initials(name: string): string {
  const letters = name
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((w) => w[0]!.toUpperCase())
  return (letters.slice(0, 2).join('') || '?').slice(0, 2)
}

/** The rail's agent switcher (shell spec "Agent switcher"). */
export function AgentSwitcher() {
  const agents = useAgents()
  const activeAgent = useShell((s) => s.activeAgent)
  const chooseAgent = useShell((s) => s.chooseAgent)
  const navigate = useNavigate()
  const shown = activeAgent ?? agents.data?.[0]?.id ?? 'main'
  const label = t('webShell.agentSwitcher', { agent: shown })

  return (
    <DropdownMenu>
      <SimpleTooltip label={label} side="right">
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            aria-label={label}
            className="group/avatar rounded-full hover:bg-transparent data-[state=open]:bg-transparent"
          >
            <span
              aria-hidden
              className="rail-avatar grid size-7 place-items-center rounded-full text-small font-semibold text-text"
            >
              {initials(shown)}
            </span>
          </Button>
        </DropdownMenuTrigger>
      </SimpleTooltip>
      <DropdownMenuContent side="right" align="start" sideOffset={8} className="min-w-56">
        <DropdownMenuLabel>{t('webShell.agentSwitcherLabel')}</DropdownMenuLabel>
        {agents.isPending ? (
          <DropdownMenuItem disabled>{t('webShell.agentsLoading')}</DropdownMenuItem>
        ) : agents.isError ? (
          <DropdownMenuItem disabled>{t('webShell.agentsFailed')}</DropdownMenuItem>
        ) : (
          agents.data.map((agent) => (
            <DropdownMenuItem
              key={agent.id}
              onSelect={() => chooseAgent(agent.id)}
              aria-checked={activeAgent === agent.id}
              role="menuitemradio"
            >
              <span
                aria-hidden
                className="grid size-6 shrink-0 place-items-center rounded-full border border-border bg-surface-2 text-small font-semibold text-text-muted"
              >
                {initials(agent.name || agent.id)}
              </span>
              <span className="min-w-0 flex-1 truncate">{agent.name}</span>
              <Check
                className={activeAgent === agent.id ? 'text-accent! opacity-100' : 'opacity-0'}
                aria-hidden
              />
            </DropdownMenuItem>
          ))
        )}
        {activeAgent ? (
          <DropdownMenuItem onSelect={() => chooseAgent(null)}>
            <span aria-hidden className="size-6 shrink-0" />
            {t('webShell.agentFilterAll')}
          </DropdownMenuItem>
        ) : null}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => navigate('/settings/agents')}>
          <Settings2 aria-hidden />
          {t('webShell.agentManage')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
