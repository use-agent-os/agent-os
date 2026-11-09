import { Check } from 'lucide-react'
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
            className="rounded-lg bg-surface-3 font-semibold"
          >
            <span aria-hidden className="text-small">
              {initials(shown)}
            </span>
          </Button>
        </DropdownMenuTrigger>
      </SimpleTooltip>
      <DropdownMenuContent side="right" align="start">
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
              <Check
                className={activeAgent === agent.id ? 'opacity-100' : 'opacity-0'}
                aria-hidden
              />
              {agent.name}
            </DropdownMenuItem>
          ))
        )}
        {activeAgent ? (
          <DropdownMenuItem onSelect={() => chooseAgent(null)}>
            <Check className="opacity-0" aria-hidden />
            {t('webShell.agentFilterAll')}
          </DropdownMenuItem>
        ) : null}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => navigate('/settings/agents')}>
          {t('webShell.agentManage')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
