import { Bot, RotateCcw, User } from 'lucide-react'
import { useId } from 'react'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { shortAge } from '~/lib/relative-time'
import { useNow } from '~/lib/use-now'
import { errorText, useFlowRevisions } from '~/stores/flows'
import { Floating, type Box } from './Floating'
import type { FlowAuthor, FlowRevision } from './types'

/** "just now", "5m ago". */
export function revisionAge(created: string, now: number): string {
  const at = Date.parse(created)
  if (!Number.isFinite(at)) return ''
  const age = shortAge(at, now)
  return age === 'now' ? t('flows.history.now') : t('flows.history.ago').replace('{age}', age)
}

export function AuthorBadge({ author }: { author: FlowAuthor | undefined }) {
  const agent = author === 'agent'
  const Icon = agent ? Bot : User
  return (
    <span className="flw-author" data-author={agent ? 'agent' : 'user'}>
      <Icon className="size-3" strokeWidth={2} aria-hidden />
      {agent ? t('flows.history.agent') : t('flows.history.you')}
    </span>
  )
}

/**
 * The toolbar's "Version history": every saved version of the flow, newest
 * first, with who saved it (you or the agent from chat), when, and what
 * changed. Any version but the current one can be restored; restoring saves
 * a new version, so it can itself be undone from here.
 */
export function VersionHistory({
  flowId,
  currentRev,
  anchor,
  canRestore,
  blockedReason,
  onRestore,
  onClose,
}: {
  flowId: string
  currentRev: number
  anchor: Box
  canRestore: boolean
  /** Why Restore is off (writes refused), shown on the buttons. */
  blockedReason: string | null
  onRestore: (rev: FlowRevision) => void
  onClose: () => void
}) {
  const revisions = useFlowRevisions(flowId, true)
  const now = useNow(30_000)
  const list = revisions.data ?? []
  return (
    <Floating
      anchor={anchor}
      onClose={onClose}
      label={t('flows.history.title')}
      className="flw-pop--history"
    >
      <div className="flw-pop__head">{t('flows.history.title')}</div>
      <div className="flw-pop__list" role="list" aria-label={t('flows.history.title')}>
        {revisions.isLoading ? (
          <p className="flw-pop__empty">{t('flows.history.loading')}</p>
        ) : null}
        {revisions.isError ? (
          <p className="flw-pop__empty">
            {t('flows.history.failed')} · {errorText(revisions.error)}
          </p>
        ) : null}
        {revisions.isSuccess && list.length === 0 ? (
          <p className="flw-pop__empty">{t('flows.history.empty')}</p>
        ) : null}
        {list.map((rev) => {
          const current = rev.rev === currentRev
          return (
            <div
              key={rev.rev}
              role="listitem"
              className="flw-rev"
              data-current={current || undefined}
            >
              <div className="flw-rev__line">
                <span className="flw-rev__num">
                  {t('flows.history.version').replace('{rev}', String(rev.rev))}
                </span>
                <AuthorBadge author={rev.author} />
                <span
                  className="flw-rev__age"
                  title={
                    Number.isFinite(Date.parse(rev.created))
                      ? new Date(rev.created).toLocaleString()
                      : undefined
                  }
                >
                  {revisionAge(rev.created, now)}
                </span>
                <span className="flex-1" />
                {current ? (
                  <span className="flw-rev__current">{t('flows.history.current')}</span>
                ) : (
                  <button
                    type="button"
                    className="flw-rev__restore"
                    disabled={!canRestore}
                    title={blockedReason ?? undefined}
                    aria-label={`${t('flows.history.restore')} ${t('flows.history.version').replace('{rev}', String(rev.rev))}`}
                    onClick={() => onRestore(rev)}
                  >
                    <RotateCcw className="size-3" strokeWidth={2} aria-hidden />
                    {t('flows.history.restore')}
                  </button>
                )}
              </div>
              {rev.summary ? <p className="flw-rev__summary">{rev.summary}</p> : null}
            </div>
          )
        })}
      </div>
    </Floating>
  )
}

/** Asked before a restore: what it does, and that it is undoable. */
export function RestoreVersionConfirm({
  rev,
  busy,
  onCancel,
  onConfirm,
}: {
  rev: number
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={onCancel}
      dismissible={!busy}
      overlayClassName="flw-alert__overlay"
      className="flw-alert"
    >
      <h2 id={titleId} className="flw-alert__title">
        {t('flows.history.confirm.title').replace('{rev}', String(rev))}
      </h2>
      <p id={bodyId} className="flw-alert__body">
        {t('flows.history.confirm.body')}
      </p>
      <div className="flw-alert__actions">
        <Button disabled={busy} onClick={onCancel}>
          {t('flows.history.confirm.cancel')}
        </Button>
        <Button variant="primary" disabled={busy} onClick={onConfirm}>
          {t('flows.history.confirm.restore')}
        </Button>
      </div>
    </ModalShell>
  )
}
