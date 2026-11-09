import {
  BrainCircuit,
  CheckCircle2,
  ChevronRight,
  FileText,
  FolderOpen,
  Globe,
  Loader2,
  PanelRightOpen,
  PenLine,
  Search,
  Terminal,
  XCircle,
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

/** One tool call: a compact row that expands to its arguments and result. */
export function ToolRow({ item }: { item: ToolItem }) {
  const [open, setOpen] = useState(false)
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

  return (
    <div
      data-tool-state={item.state}
      className={cn(
        'rounded-md border text-[length:var(--text-prose)]',
        item.state === 'failed' ? 'border-danger/50' : 'border-border',
      )}
    >
      <button
        type="button"
        aria-expanded={open}
        aria-controls={regionId}
        onClick={() => setOpen((v) => !v)}
        className="target flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-ui"
      >
        <ChevronRight
          className={cn(
            'size-3.5 shrink-0 text-text-muted transition-transform',
            open && 'rotate-90',
          )}
          aria-hidden
        />
        <Icon className="size-4 shrink-0 text-text-muted" aria-hidden />
        <span className="shrink-0 font-medium">{label}</span>
        {detail ? (
          <span className="min-w-0 flex-1 truncate font-mono text-small text-text-muted">
            {detail}
          </span>
        ) : (
          <span className="flex-1" />
        )}
        {item.state === 'running' ? (
          <span className="flex shrink-0 items-center gap-1 text-small text-text-muted">
            <Loader2 className="motion-status size-3.5 animate-spin" aria-hidden data-status-icon />
            {t('webChat.toolRunning')}
          </span>
        ) : item.state === 'failed' ? (
          <span className="flex shrink-0 items-center gap-1 text-small text-danger">
            <XCircle className="size-3.5" aria-hidden data-status-icon />
            {t('webChat.toolFailed')}
          </span>
        ) : (
          <span className="flex shrink-0 items-center gap-1 text-small text-text-muted">
            <CheckCircle2 className="size-3.5 text-ok" aria-hidden data-status-icon />
            <span className="sr-only">{t('webChat.toolSucceeded')}</span>
          </span>
        )}
        {duration ? (
          <span className="shrink-0 text-small text-text-muted tabular-nums">{duration}</span>
        ) : null}
      </button>
      {open ? (
        <div
          id={regionId}
          role="region"
          aria-label={t('webChat.toolDetails', { tool: label })}
          className="flex flex-col gap-2 border-t border-border px-3 py-2"
        >
          {item.args !== undefined ? (
            <div>
              <p className="text-small font-medium text-text-muted">{t('webChat.toolArguments')}</p>
              <pre className="scroll-contained max-h-48 rounded bg-code-bg p-2 font-mono text-small whitespace-pre-wrap">
                {pretty(item.args)}
              </pre>
            </div>
          ) : null}
          {item.result !== undefined ? (
            <div>
              <p className="text-small font-medium text-text-muted">{t('webChat.toolResult')}</p>
              <pre
                data-testid="tool-result"
                className="scroll-contained max-h-64 rounded bg-code-bg p-2 font-mono text-small whitespace-pre-wrap"
              >
                {long ? `${result.slice(0, INLINE_RESULT_LIMIT)}…` : result}
              </pre>
              {long ? (
                <Button
                  size="sm"
                  variant="outline"
                  className="mt-1"
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
      ) : null}
    </div>
  )
}
