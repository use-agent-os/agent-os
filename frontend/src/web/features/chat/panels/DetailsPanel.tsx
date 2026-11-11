import { FileText, X } from 'lucide-react'
import { Link } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { artifactName } from '@/views/chat/transcript/artifacts'
import { sessionRunStatus } from '@/views/sessions/logic'
import { cn } from '@/lib/utils'
import { Button } from '@/web/components/ui/button'
import { Card } from '@/web/components/ui/card'
import { useProjectNames, useSessionsList } from '@/web/shell/session-data'
import {
  sessionAgent,
  sessionChannel,
  sessionParent,
  sessionProject,
  sessionTitle,
  sessionUpdatedMs,
  type SessionRow,
} from '@/web/shell/session-list'
import { sessionKeyToPath } from '@/web/shell/session-url'
import { usePlanMode, useSessionUsage, useUnpinnedRoute } from '../composer/capabilities'
import type { TranscriptState } from '../model/types'

/** One term of a group: label and value side by side on the group's grid. */
function Row({
  label,
  children,
  className,
}: {
  label: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <>
      <dt className="pt-px text-label text-text-muted">{label}</dt>
      <dd className={cn('min-w-0 break-words', className)}>{children}</dd>
    </>
  )
}

/** A group of terms: a definition grid on a raised well. */
function Group({ children }: { children: React.ReactNode }) {
  return (
    <Card asChild variant="plain" padding="sm" className="bg-surface-2 shadow-highlight">
      <dl className="grid grid-cols-[6.75rem_minmax(0,1fr)] items-baseline gap-x-3 gap-y-2.5">
        {children}
      </dl>
    </Card>
  )
}

function when(ms: number | null | undefined): string {
  return ms ? new Date(ms).toLocaleString() : t('webChat.detailsNone')
}

/** The open session's context (chat spec "Details column"). */
export function DetailsPanel({
  sessionKey,
  row,
  transcript,
  onClose,
}: {
  sessionKey: string
  row: SessionRow | undefined
  transcript: TranscriptState
  onClose(): void
}) {
  const usage = useSessionUsage(sessionKey)
  const unpinned = useUnpinnedRoute()
  const plan = usePlanMode(sessionKey)
  const projects = useProjectNames()
  const sessions = useSessionsList()
  const children = sessions.rows.filter((r) => sessionParent(r) === sessionKey)
  const artifacts = transcript.turns.flatMap((turn) =>
    turn.items.filter((i) => i.kind === 'artifact' || i.kind === 'card'),
  )
  const project = row ? sessionProject(row) : null
  const route = transcript.turns
    .flatMap((t) => t.items)
    .filter((i) => i.kind === 'routerDecision')
    .at(-1)
  const created = row ? (row.created_at as string | number | undefined) : undefined
  const task = row ? sessionRunStatus(row) : 'idle'

  return (
    <aside
      aria-label={t('webChat.detailsTitle')}
      className="flex h-full w-full flex-col bg-surface-1"
    >
      <header className="flex min-h-14 items-center gap-2 border-b border-border px-4 py-2">
        <h2 className="flex-1 text-section">{t('webChat.detailsTitle')}</h2>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label={t('webChat.detailsClose')}
          onClick={onClose}
          className="text-text-muted hover:text-text"
        >
          <X />
        </Button>
      </header>
      <div className="scroll-contained anim-stagger flex flex-1 flex-col gap-3 p-4">
        <Group>
          <Row label={t('webChat.detailsAgent')}>{row ? sessionAgent(row) : '—'}</Row>
          <Row label={t('webChat.detailsRoute')} className="font-mono text-small">
            {route && route.kind === 'routerDecision'
              ? `${route.tier ? `${route.tier} · ` : ''}${route.model}`
              : (row?.model ?? unpinned)}
          </Row>
          <Row label={t('webChat.detailsChannel')}>{row ? sessionChannel(row) : '—'}</Row>
          <Row label={t('webChat.detailsProject')}>
            {project ? (projects.data?.get(project) ?? project) : t('webChat.detailsNone')}
          </Row>
        </Group>
        <Group>
          <Row label={t('webChat.detailsCreated')} className="tabular-nums">
            {when(
              created
                ? typeof created === 'number'
                  ? created < 1e10
                    ? created * 1000
                    : created
                  : Date.parse(created)
                : null,
            )}
          </Row>
          <Row label={t('webChat.detailsUpdated')} className="tabular-nums">
            {when(row ? sessionUpdatedMs(row) : null)}
          </Row>
          <Row label={t('webChat.detailsUsage')} className="tabular-nums">
            {usage.data ? `${(usage.data.input + usage.data.output).toLocaleString()} tokens` : '—'}
          </Row>
          <Row label={t('webChat.detailsCost')} className="tabular-nums">
            {usage.data?.cost !== null && usage.data?.cost !== undefined
              ? `$${usage.data.cost.toFixed(4)}`
              : '—'}
          </Row>
        </Group>
        <Group>
          <Row label={t('webChat.detailsTask')}>{task}</Row>
          <Row label={t('webChat.detailsSubagents')}>
            {children.length ? (
              <ul className="flex flex-col gap-1">
                {children.map((child) => (
                  <li key={child.key}>
                    <Link
                      to={sessionKeyToPath(child.key)}
                      className="text-accent underline-offset-2 hover:underline"
                    >
                      {sessionTitle(child)}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              t('webChat.detailsNone')
            )}
          </Row>
          <Row label={t('webChat.detailsPlan')}>
            {plan.on ? t('webChat.planOn') : t('webChat.planOff')}
          </Row>
          <Row label={t('webChat.detailsArtifacts')}>
            {artifacts.length ? (
              <ul className="flex flex-col gap-1">
                {artifacts.map((item) =>
                  item.kind === 'artifact' || item.kind === 'card' ? (
                    <li key={item.id} className="flex items-center gap-1.5">
                      <FileText className="size-3.5 text-text-muted" aria-hidden />
                      <span className="truncate">{artifactName(item.artifact)}</span>
                    </li>
                  ) : null,
                )}
              </ul>
            ) : (
              t('webChat.detailsNone')
            )}
          </Row>
          <Row label={t('webChat.detailsQueued')} className="tabular-nums">
            {transcript.queued}
          </Row>
        </Group>
      </div>
    </aside>
  )
}
