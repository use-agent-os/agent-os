import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useSessions, type SessionRow } from '~/stores/sessions'
import { Notice, Pill } from '../parts'
import { MEMORY_KEY, errorText } from './api'
import { AGENT_ID, documentName, forgetCounts, type ForgetReport } from './logic'
import { Sheet, Spinner, count, fill } from './ui'

const MANUAL = '__manual__'

/** The gateway's NOT_FOUND names the selector rules; say it in plain words. */
function forgetError(error: unknown): string {
  const code = (error as { code?: unknown } | null)?.code
  return code === 'NOT_FOUND' ? t('settings.memory.forget.notFound') : errorText(error)
}

/** The agent a session belongs to: its own field, else the `agent:<id>:` key prefix. */
function sessionAgent(row: SessionRow): string {
  const own = row.raw.agent_id ?? row.raw.agentId
  if (own) return String(own)
  const match = /^agent:([^:]+):/.exec(row.key)
  return match ? match[1]! : AGENT_ID
}

function sessionLabel(row: SessionRow): string {
  const when = row.updatedAt
    ? new Date(row.updatedAt).toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
    : ''
  return when ? `${row.title} · ${when}` : row.title
}

/**
 * Forget a conversation: preview (`memory.forget` with `dryRun`) what one
 * session taught the agent, then remove it only on an explicit second step.
 */
export function ForgetSheet({ onClose }: { onClose: () => void }) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const sessions = useSessions()
  const rows = sessions.rows.filter((row) => sessionAgent(row) === AGENT_ID)
  const [choice, setChoice] = useState('')
  const [manual, setManual] = useState('')
  const [preview, setPreview] = useState<ForgetReport | null>(null)
  const [done, setDone] = useState<ForgetReport | null>(null)
  const selectId = useId()
  const manualId = useId()
  const selector = (choice === MANUAL ? manual : choice).trim()

  const run = useMutation({
    mutationFn: (dryRun: boolean) =>
      rpc.call<ForgetReport>('memory.forget', { agentId: AGENT_ID, sessions: [selector], dryRun }),
    onSuccess: async (report, dryRun) => {
      if (dryRun) {
        setPreview(report)
        return
      }
      setDone(report)
      const n = forgetCounts(report)
      toast.success(fill('settings.memory.forget.done', { facts: n.facts, notes: n.notes }), {
        id: 'stg-memory-forget',
      })
      await queryClient.invalidateQueries({ queryKey: MEMORY_KEY })
    },
  })

  const report = done ?? preview
  const counts = report ? forgetCounts(report) : null
  const busy = run.isPending

  const foot = done ? (
    <Button variant="primary" onClick={onClose}>
      {t('settings.memory.close')}
    </Button>
  ) : preview ? (
    <>
      <Button
        disabled={busy}
        onClick={() => {
          setPreview(null)
          run.reset()
        }}
      >
        {t('settings.memory.forget.back')}
      </Button>
      <Button
        variant="danger"
        disabled={busy || !counts || counts.total === 0}
        onClick={() => run.mutate(false)}
      >
        {busy ? t('settings.memory.forget.forgetting') : t('settings.memory.forget.confirm')}
      </Button>
    </>
  ) : (
    <>
      <Button disabled={busy} onClick={onClose}>
        {t('settings.memory.cancel')}
      </Button>
      <Button variant="primary" disabled={busy || !selector} onClick={() => run.mutate(true)}>
        {busy ? t('settings.memory.forget.checking') : t('settings.memory.forget.preview')}
      </Button>
    </>
  )

  return (
    <Sheet
      title={t('settings.memory.forget.title')}
      blurb={t('settings.memory.forget.blurb')}
      dismissible={!busy}
      onClose={onClose}
      foot={foot}
    >
      {!report ? (
        <div className="mem-form">
          <label htmlFor={selectId} className="mem-form__label">
            {t('settings.memory.forget.session')}
          </label>
          {sessions.loading ? (
            <Spinner />
          ) : (
            <select
              id={selectId}
              className="mac-select"
              value={choice}
              disabled={busy}
              onChange={(e) => {
                setChoice(e.target.value)
                run.reset()
              }}
            >
              <option value="" disabled>
                {rows.length > 0
                  ? t('settings.memory.forget.pick')
                  : t('settings.memory.forget.noSessions')}
              </option>
              {rows.map((row) => (
                <option key={row.key} value={row.key}>
                  {sessionLabel(row)}
                </option>
              ))}
              <option value={MANUAL}>{t('settings.memory.forget.manual')}</option>
            </select>
          )}
          {choice === MANUAL ? (
            <>
              <label htmlFor={manualId} className="mem-form__label">
                {t('settings.memory.forget.manual.label')}
              </label>
              <input
                id={manualId}
                className="mac-input"
                data-mono="true"
                value={manual}
                autoComplete="off"
                spellCheck={false}
                disabled={busy}
                placeholder={t('settings.memory.forget.manual.placeholder')}
                onChange={(e) => {
                  setManual(e.target.value)
                  run.reset()
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && selector && !busy) {
                    e.preventDefault()
                    run.mutate(true)
                  }
                }}
              />
              <p className="mem-form__help">{t('settings.memory.forget.manual.help')}</p>
            </>
          ) : null}
          {run.isError ? <Notice tone="danger">{forgetError(run.error)}</Notice> : null}
        </div>
      ) : (
        <ForgetReportView report={report} applied={done !== null} error={run.error} />
      )}
    </Sheet>
  )
}

function ForgetReportView({
  report,
  applied,
  error,
}: {
  report: ForgetReport
  applied: boolean
  error: unknown
}) {
  const n = forgetCounts(report)
  const already = report.sessions?.some((s) => s.alreadyForgotten)
  return (
    <div className="mem-forget">
      {error ? <Notice tone="danger">{forgetError(error)}</Notice> : null}
      {n.total === 0 ? (
        <Notice tone="info">
          {applied ? t('settings.memory.forget.nothingLeft') : t('settings.memory.forget.nothing')}
        </Notice>
      ) : (
        <Notice tone={applied ? 'ok' : 'warn'}>
          {fill(
            applied ? 'settings.memory.forget.summary.done' : 'settings.memory.forget.summary',
            {
              facts: count(n.facts),
              notes: count(n.notes),
              passages: count(n.passages),
            },
          )}
        </Notice>
      )}
      {already ? <p className="mem-quiet">{t('settings.memory.forget.already')}</p> : null}

      {report.curated && report.curated.length > 0 ? (
        <section className="mem-forget__group">
          <h3>{t('settings.memory.forget.facts')}</h3>
          <ul>
            {report.curated.map((item, i) => (
              <li key={`${item.target}:${i}`} className="mem-forget__item">
                <Pill>
                  {item.target === 'user'
                    ? t('settings.memory.you.title')
                    : t('settings.memory.work.title')}
                </Pill>
                <span>{item.entry}</span>
                {item.mixed ? (
                  <span className="mem-forget__mixed">{t('settings.memory.forget.mixed')}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {report.notes && report.notes.length > 0 ? (
        <section className="mem-forget__group">
          <h3>{t('settings.memory.forget.notes')}</h3>
          <ul>
            {report.notes.map((note, i) => (
              <li key={`${note.path}:${i}`} className="mem-forget__item">
                <span className="mem-forget__path">{documentName(note.path)}</span>
                <span className="mem-quiet">
                  {fill('settings.memory.forget.noteChars', { chars: count(note.chars ?? 0) })}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {report.errors && report.errors.length > 0 ? (
        <Notice tone="danger">{report.errors.map((e) => e.message || e.code).join(' ')}</Notice>
      ) : null}
      {report.notCovered ? <p className="mem-fine">{report.notCovered}</p> : null}
    </div>
  )
}
