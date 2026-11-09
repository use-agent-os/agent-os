import {
  AlertTriangle,
  Inbox as InboxIcon,
  MessageCircleQuestion,
  RefreshCw,
  ShieldAlert,
} from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router'
import { useRpc } from '@/app/providers'
import { formatNumber, getLocale, t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesOverview'
import { composeAskAnswer, type AskQuestion } from '@/lib/ask-questions'
import type { AttentionItem } from '@/lib/attention-inbox'
import { gate, requireConnection, type Gate } from '@/lib/disabled-reason'
import { usePendingQuestions } from '@/lib/pending-questions'
import {
  useDismissedIssues,
  type IssueAction,
  type RuntimeIssue,
  type SettingsTarget,
} from '@/lib/runtime-issues'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { useAttentionInbox } from '@/lib/useAttentionInbox'
import { cn } from '@/lib/utils'
import { approvalMonitor, useApprovals } from '@/services/approval-monitor'
import { Button } from '@/web/components/ui/button'
import { PageHeader } from '@/web/components/ui/page-header'
import { EmptyState, StaleBadge } from '@/web/components/ui/states'
import { sendOnce } from '@/web/features/chat/send'
import { ApprovalCard } from '@/web/shell/ApprovalTray'
import { sessionKeyToPath } from '@/web/shell/session-url'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'

// Inbox: everything waiting on the operator (useAttentionInbox), each answered
// in place. Approvals with the same command are grouped and every occurrence
// keeps its own decisions; `ask_user` questions are answered by sending the
// composed answer as the session's next message; runtime issues carry their
// own actions.

/** Where a runtime issue's "Open settings" lands; null when there is no such page. */
function settingsPath(target: SettingsTarget | undefined): string | null {
  switch (target) {
    case 'connection':
      return '/settings/gateway'
    case 'providers':
      return '/settings/models'
    case 'channels':
      return '/settings/channels'
    default:
      // The web console cannot drive the engine updater.
      return null
  }
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? '')
}

/** When the item started waiting, as a wall-clock time: no timer needed. */
function Since({ at }: { at: number | null }) {
  if (at === null) return null
  const time = new Intl.DateTimeFormat(getLocale(), { hour: '2-digit', minute: '2-digit' }).format(
    new Date(at),
  )
  return (
    <span className="text-small text-text-muted">{t('webPagesOverview.inboxSince', { time })}</span>
  )
}

function ItemCard({
  item,
  icon,
  kind,
  tone,
  children,
}: {
  item: AttentionItem
  icon: ReactNode
  kind: string
  tone: string
  children: ReactNode
}) {
  return (
    <article
      aria-label={t('webPagesOverview.inboxItemLabel', { kind, title: item.title })}
      data-kind={item.kind}
      className={cn(
        'flex min-w-0 flex-col gap-3 rounded-lg border border-l-4 border-border bg-surface-1 p-4',
        tone,
      )}
    >
      <header className="flex flex-wrap items-start gap-2">
        <span className="mt-0.5 text-text-muted [&_svg]:size-4" aria-hidden>
          {icon}
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-small text-text-muted">{kind}</p>
          <h2 className="font-semibold break-words">{item.title}</h2>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {item.sessions.length > 1 ? (
            <span className="rounded-full border border-border px-2 py-0.5 text-small">
              {tPlural('webPagesOverview.inboxSeenIn', item.sessions.length)}
            </span>
          ) : null}
          <Since at={item.createdAt} />
        </div>
      </header>
      {children}
    </article>
  )
}

function ApprovalItem({ item }: { item: AttentionItem }) {
  const namespace = item.approval?.namespace || 'exec'
  return (
    <ItemCard
      item={item}
      icon={<ShieldAlert />}
      kind={t('webPagesOverview.inboxKindApproval', { namespace })}
      tone="border-l-warn"
    >
      <ul role="list" className="flex flex-col gap-2">
        {item.members.map((member) =>
          member.approval ? <ApprovalCard key={member.id} item={member.approval} /> : null,
        )}
      </ul>
      <Link
        to="/settings/approvals"
        className="self-start text-small text-accent underline-offset-2 hover:underline"
      >
        {t('webPagesOverview.inboxApprovalSettings')}
      </Link>
    </ItemCard>
  )
}

function QuestionItem({ item, answer }: { item: AttentionItem; answer: Gate }) {
  const rpc = useRpc()
  const questions: AskQuestion[] = item.questions ?? []
  const [picks, setPicks] = useState<string[][]>(() => questions.map(() => []))
  const [freeText, setFreeText] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const composed = composeAskAnswer(questions, picks, freeText)
  const sessions = item.members.map((m) => m.sessionKey).filter((k): k is string => !!k)

  const toggle = (qi: number, label: string, multi: boolean) =>
    setPicks((prev) =>
      prev.map((chosen, i) => {
        if (i !== qi) return chosen
        if (chosen.includes(label)) return chosen.filter((l) => l !== label)
        return multi ? [...chosen, label] : [label]
      }),
    )

  // The answer is the session's next user message (ask_user ends the turn).
  const send = async () => {
    if (!composed || sending || answer.disabled || sessions.length === 0) return
    setSending(true)
    setError(null)
    const elevated = useApprovals.getState().elevatedMode
    try {
      for (const sessionKey of sessions) {
        const params: Record<string, unknown> = { message: composed, sessionKey }
        if (elevated) params._source = { elevated }
        const outcome = await sendOnce(rpc, 'chat.send', params)
        if (outcome.status !== 'sent') throw outcome.error
        usePendingQuestions.getState().clearSession(sessionKey)
      }
    } catch (e) {
      setError(t('webPagesOverview.inboxSendFailed', { message: errorText(e) }))
    } finally {
      setSending(false)
    }
  }

  const sendDisabled = answer.disabled || sending || !composed
  return (
    <ItemCard
      item={item}
      icon={<MessageCircleQuestion />}
      kind={t('webPagesOverview.inboxKindQuestion')}
      tone="border-l-info"
    >
      {questions.map((q, qi) => (
        <fieldset key={`${qi}:${q.question}`} className="flex flex-col gap-1.5">
          <legend className="mb-1 font-medium">{q.question}</legend>
          <div className="flex flex-wrap gap-1.5">
            {q.options.map((opt) => {
              const on = picks[qi]?.includes(opt.label) ?? false
              return (
                <Button
                  key={opt.label}
                  size="sm"
                  variant={on ? 'primary' : 'outline'}
                  aria-pressed={on}
                  title={opt.description}
                  disabled={answer.disabled || sending}
                  onClick={() => toggle(qi, opt.label, q.multiSelect)}
                >
                  {opt.label}
                </Button>
              )
            })}
          </div>
        </fieldset>
      ))}
      <label className="flex flex-col gap-1">
        <span className="text-small text-text-muted">{t('webPagesOverview.inboxFreeText')}</span>
        <input
          type="text"
          value={freeText}
          disabled={answer.disabled || sending}
          placeholder={t('webPagesOverview.inboxFreeTextPlaceholder')}
          onChange={(e) => setFreeText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              void send()
            }
          }}
          className="h-9 rounded-md border border-border-control bg-surface-0 px-3"
        />
      </label>
      {error ? (
        <p role="alert" className="text-small text-danger">
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" size="sm" disabled={sendDisabled} onClick={() => void send()}>
          {sending
            ? t('webPagesOverview.inboxSending')
            : sessions.length > 1
              ? tPlural('webPagesOverview.inboxSendToMany', sessions.length)
              : t('webPagesOverview.inboxSend')}
        </Button>
        {answer.reason ? <span className="text-small text-text-muted">{answer.reason}</span> : null}
        <span className="flex min-w-0 flex-wrap gap-2">
          {sessions.map((key) => (
            <Link
              key={key}
              to={sessionKeyToPath(key)}
              title={t('webPagesOverview.inboxOpenSession', { session: key })}
              className="max-w-full truncate font-mono text-small text-accent underline-offset-2 hover:underline"
            >
              {key}
            </Link>
          ))}
        </span>
      </div>
    </ItemCard>
  )
}

function IssueItem({ item, issue }: { item: AttentionItem; issue: RuntimeIssue }) {
  const navigate = useNavigate()
  const retry = useRuntimeSignals((s) => s.retry)
  const dismiss = useDismissedIssues((s) => s.dismiss)
  const [stepsOpen, setStepsOpen] = useState(false)

  const run = (action: IssueAction) => {
    switch (action.kind) {
      case 'retry_connection':
        retry()
        return
      case 'open_fix_steps':
        if (issue.fixSteps.length > 0) setStepsOpen((open) => !open)
        else void navigate('/settings/health')
        return
      case 'open_settings': {
        const path = settingsPath(action.settings)
        if (path) void navigate(path)
        return
      }
      case 'dismiss':
        dismiss(issue.id)
        return
      default:
        // restart_gateway is only offered to a host that can run it.
        return
    }
  }

  // Only the actions this console can carry out are offered.
  const actions = issue.actions.filter(
    (a) =>
      a.kind !== 'restart_gateway' &&
      (a.kind !== 'open_settings' || settingsPath(a.settings) !== null),
  )
  const error = issue.severity === 'error'
  return (
    <ItemCard
      item={item}
      icon={<AlertTriangle />}
      kind={
        error ? t('webPagesOverview.inboxKindIssueError') : t('webPagesOverview.inboxKindIssueWarn')
      }
      tone={error ? 'border-l-danger' : 'border-l-warn'}
    >
      {issue.detail ? <p className="text-text-muted break-words">{issue.detail}</p> : null}
      {issue.findingId ? (
        <code className="self-start font-mono text-small text-text-muted">{issue.findingId}</code>
      ) : null}
      {stepsOpen && issue.fixSteps.length > 0 ? (
        <ol aria-label={t('webPagesOverview.inboxFixSteps')} className="flex flex-col gap-2">
          {issue.fixSteps.map((step, i) => (
            <li key={`${i}:${step.label ?? ''}`} className="flex gap-2">
              <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-surface-3 text-small">
                {formatNumber(i + 1)}
              </span>
              <div className="min-w-0 flex-1">
                {step.label ? <p className="font-medium">{step.label}</p> : null}
                {step.command ? (
                  <code
                    tabIndex={0}
                    className="block overflow-x-auto rounded bg-surface-0 px-2 py-1 font-mono text-small whitespace-pre"
                  >
                    {step.command}
                  </code>
                ) : null}
                {step.detail ? <p className="text-small text-text-muted">{step.detail}</p> : null}
              </div>
            </li>
          ))}
        </ol>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        {actions.map((a) => (
          <Button
            key={a.kind}
            size="sm"
            variant={a.kind === 'dismiss' ? 'ghost' : 'outline'}
            aria-expanded={
              a.kind === 'open_fix_steps' && issue.fixSteps.length > 0 ? stepsOpen : undefined
            }
            onClick={() => run(a)}
          >
            {a.label}
          </Button>
        ))}
        {issue.fixSteps.length > 0 ? (
          <Link
            to="/settings/health"
            className="text-small text-accent underline-offset-2 hover:underline"
          >
            {t('webPagesOverview.inboxOpenHealth')}
          </Link>
        ) : null}
      </div>
    </ItemCard>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col rounded-lg border border-border bg-surface-1 px-3 py-2">
      <dt className="text-small text-text-muted">{label}</dt>
      <dd className="text-lg font-semibold tabular-nums">{formatNumber(value)}</dd>
    </div>
  )
}

export function InboxPage() {
  const { items, issues } = useAttentionInbox()
  const connection = useDisplayedConnection()
  const everConnected = useRuntimeSignals((s) => s.everConnected)
  const refresh = useRuntimeSignals((s) => s.refresh)
  const dismissed = useDismissedIssues((s) => s.ids)
  const restoreAll = useDismissedIssues((s) => s.restoreAll)
  const [rechecking, setRechecking] = useState(false)
  const answer = gate(requireConnection(connection))
  const live = connection === 'connected'

  const recheck = async () => {
    if (rechecking) return
    setRechecking(true)
    try {
      refresh()
      await approvalMonitor.pollNow()
    } finally {
      setRechecking(false)
    }
  }

  const count = (kind: AttentionItem['kind']) =>
    items.filter((i) => i.kind === kind).reduce((n, i) => n + i.members.length, 0)
  const hiddenIssues = issues.filter((i) => dismissed.includes(i.id)).length

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-4 p-4 md:p-8">
      <PageHeader
        title={t('webPagesOverview.inboxTitle')}
        badge={!live && everConnected ? <StaleBadge /> : null}
        description={t('webPagesOverview.inboxSubtitle')}
        actions={
          <Button variant="outline" disabled={rechecking} onClick={() => void recheck()}>
            <RefreshCw className={rechecking ? 'animate-spin' : undefined} aria-hidden />
            {t('webPagesOverview.inboxRecheck')}
          </Button>
        }
      />

      <dl
        aria-label={t('webPagesOverview.inboxSummary')}
        className="grid grid-cols-3 gap-2 sm:max-w-md"
      >
        <Stat label={t('webPagesOverview.inboxApprovals')} value={count('approval')} />
        <Stat label={t('webPagesOverview.inboxQuestions')} value={count('question')} />
        <Stat label={t('webPagesOverview.inboxIssues')} value={count('issue')} />
      </dl>

      {items.length === 0 ? (
        <EmptyState
          icon={<InboxIcon />}
          title={live ? t('webPagesOverview.inboxEmpty') : t('webPagesOverview.inboxEmptyUnknown')}
          description={
            live
              ? t('webPagesOverview.inboxEmptyHint')
              : t('webPagesOverview.inboxEmptyUnknownHint')
          }
          action={
            <Button variant="outline" disabled={rechecking} onClick={() => void recheck()}>
              {t('webPagesOverview.inboxRecheck')}
            </Button>
          }
        />
      ) : (
        <section aria-label={t('webPagesOverview.inboxList')} className="flex flex-col gap-3">
          {items.map((item) =>
            item.kind === 'approval' ? (
              <ApprovalItem key={item.id} item={item} />
            ) : item.kind === 'question' ? (
              <QuestionItem key={item.id} item={item} answer={answer} />
            ) : item.issue ? (
              <IssueItem key={item.id} item={item} issue={item.issue} />
            ) : null,
          )}
        </section>
      )}

      <footer className="flex flex-col gap-1 text-small text-text-muted">
        <p>{t('webPagesOverview.inboxQuestionsGap')}</p>
        {hiddenIssues > 0 ? (
          <p>
            {tPlural('webPagesOverview.inboxDismissed', hiddenIssues)}{' '}
            <Button variant="link" size="sm" className="h-auto px-0" onClick={restoreAll}>
              {t('webPagesOverview.inboxRestore')}
            </Button>
          </p>
        ) : null}
      </footer>
    </div>
  )
}
