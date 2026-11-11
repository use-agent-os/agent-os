import {
  BrainCircuit,
  Check,
  ChevronRight,
  FileText,
  FolderOpen,
  Globe,
  Loader2,
  PanelRightOpen,
  PenLine,
  Search,
  Terminal,
  X,
  Zap,
} from 'lucide-react'
import { useId, useState } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { cn } from '@/lib/utils'
import { fmtToolDuration, toolDisplayName, toolIconName } from '@/views/chat/transcript/tools'
import { Button } from '@/web/components/ui/button'
import type { Item } from '../model/types'
import { useChatActions } from './context'

type ToolItem = Extract<Item, { kind: 'toolCall' }>

/** Results longer than this show a preview and Open in side panel. */
export const INLINE_RESULT_LIMIT = 2_000

const ICONS = {
  terminal: Terminal,
  'file-text': FileText,
  'pen-line': PenLine,
  search: Search,
  globe: Globe,
  'folder-open': FolderOpen,
  'brain-circuit': BrainCircuit,
  zap: Zap,
} as const

/** A short detail for the row: the command, query, URL or path. */
export function toolDetail(args: unknown): string {
  if (!args || typeof args !== 'object') return ''
  const a = args as Record<string, unknown>
  const value = a.command ?? a.cmd ?? a.query ?? a.q ?? a.url ?? a.path ?? a.file_path ?? a.pattern
  if (typeof value === 'string') return value.split('\n')[0]!.slice(0, 160)
  if (Array.isArray(value)) return value.map(String).join(' ').slice(0, 160)
  return ''
}

function pretty(value: unknown): string {
  if (value === undefined) return ''
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

/**
 * One tool call: a step on the turn's activity thread that expands to its
 * arguments and result. `live`: it mounted while the turn ran, so its state
 * changes (running → done/failed) animate.
 */
export function ToolRow({ item, live = false }: { item: ToolItem; live?: boolean }) {
  const [open, setOpen] = useState(false)
  // The details mount on first expand (a long result costs nothing until
  // then) and stay, so collapsing animates too.
  const [mounted, setMounted] = useState(false)
  const actions = useChatActions()
  const regionId = useId()
  const Icon = ICONS[toolIconName(item.name)] ?? Zap
  const label = toolDisplayName(item.name, item.args)
  const detail = toolDetail(item.args)
  const duration =
    item.startedAt !== undefined && item.endedAt !== undefined
      ? fmtToolDuration(item.endedAt - item.startedAt)
      : ''
  const result = item.result ?? ''
  const long = result.length > INLINE_RESULT_LIMIT
  const running = item.state === 'running'
  const settle = live ? 'chat-pop' : undefined

  return (
    <div data-tool-state={item.state} className="text-[length:var(--text-prose)]">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={regionId}
        onClick={() => {
          setMounted(true)
          setOpen((v) => !v)
        }}
        className="chat-step-row target text-ui"
      >
        <span className="chat-node" aria-hidden>
          <Icon />
        </span>
        <span className={cn('shrink-0 font-medium', running && 'chat-shimmer')}>{label}</span>
        {detail ? (
          <span className="min-w-0 flex-1 truncate font-mono text-small text-text-muted">
            {detail}
          </span>
        ) : (
          <span className="flex-1" />
        )}
        {running ? (
          <span className="flex shrink-0 items-center gap-1.5 text-small text-text-muted">
            <Loader2
              className="motion-status size-3.5 animate-spin text-accent"
              aria-hidden
              data-status-icon
            />
            {t('webChat.toolRunning')}
          </span>
        ) : item.state === 'failed' ? (
          <span className="flex shrink-0 items-center gap-1 text-small font-medium text-danger">
            <X className={cn('size-3.5', settle)} aria-hidden data-status-icon />
            {t('webChat.toolFailed')}
          </span>
        ) : (
          <span className="flex shrink-0 items-center text-small text-ok">
            <Check className={cn('size-3.5', settle)} aria-hidden data-status-icon />
            <span className="sr-only">{t('webChat.toolSucceeded')}</span>
          </span>
        )}
        {duration ? (
          <span className="shrink-0 text-small text-text-muted tabular-nums">{duration}</span>
        ) : null}
        <ChevronRight className="chat-chevron -mr-0.5" aria-hidden />
      </button>
      {mounted ? (
        <div className="collapsible chat-collapse" data-open={open}>
          <div>
            <div
              id={regionId}
              role="region"
              aria-label={t('webChat.toolDetails', { tool: label })}
              className="chat-details"
            >
              {item.args !== undefined ? (
                <div>
                  <p className="chat-details-label">{t('webChat.toolArguments')}</p>
                  <pre tabIndex={0} className="chat-well scroll-contained max-h-48">
                    {pretty(item.args)}
                  </pre>
                </div>
              ) : null}
              {item.result !== undefined ? (
                <div>
                  <p className="chat-details-label">{t('webChat.toolResult')}</p>
                  <pre
                    data-testid="tool-result"
                    tabIndex={0}
                    className={cn(
                      'chat-well scroll-contained max-h-64',
                      item.state === 'failed' && 'border-danger/30',
                    )}
                  >
                    {long ? `${result.slice(0, INLINE_RESULT_LIMIT)}…` : result}
                  </pre>
                  {long ? (
                    <Button
                      size="sm"
                      variant="secondary"
                      className="mt-2"
                      onClick={(e) =>
                        actions.openPanel(
                          { kind: 'tool-output', id: item.id, title: label },
                          e.currentTarget,
                        )
                      }
                    >
                      <PanelRightOpen />
                      {t('webChat.toolOpenPanel')}
                    </Button>
                  ) : null}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
