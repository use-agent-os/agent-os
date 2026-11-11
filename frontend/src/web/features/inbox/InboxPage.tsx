import {
  AlertTriangle,
  CheckCheck,
  Clock,
  CloudOff,
  Copy,
  Info,
  MessageCircleQuestion,
  RefreshCw,
  ShieldAlert,
} from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { formatNumber, getLocale, t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesOverview'
import { composeAskAnswer, type AskQuestion } from '@/lib/ask-questions'
import type { AttentionItem } from '@/lib/attention-inbox'
import { copyWithFallback } from '@/lib/clipboard'
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
import { Badge } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Card } from '@/web/components/ui/card'
import { Input } from '@/web/components/ui/input'
import { PageHeader } from '@/web/components/ui/page-header'
import { EmptyState, StaleBadge } from '@/web/components/ui/states'
import { useCountUp } from '@/web/components/ui/use-count-up'
import { sendOnce } from '@/web/features/chat/send'
import {
  IconTile,
  PAGE_CLASS,
  staggerStyle,
  usePresence,
  type OverviewTone,
} from '@/web/features/home/overview-ui'
import { StatStrip } from '@/web/features/sessions/StatStrip'
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
    <span className="inline-flex items-center gap-1 text-small whitespace-nowrap text-text-muted tabular-nums">
      <Clock className="size-3.5" aria-hidden />
      {t('webPagesOverview.inboxSince', { time })}
    </span>
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
  tone: OverviewTone
  children: ReactNode
}) {
  return (
    <Card asChild padding="none">
      <article
        aria-label={t('webPagesOverview.inboxItemLabel', { kind, title: item.title })}
        data-kind={item.kind}
        data-ov-tone={tone}
        className="ov-item flex min-w-0 flex-col gap-4 p-4 sm:p-5"
      >
        <header className="flex items-start gap-3">
          <IconTile tone={tone}>{icon}</IconTile>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <Badge tone={tone === 'neutral' ? 'neutral' : tone}>{kind}</Badge>
              {item.sessions.length > 1 ? (
                <Badge>{tPlural('webPagesOverview.inboxSeenIn', item.sessions.length)}</Badge>
              ) : null}
            </div>
            <h2 className="mt-1.5 text-section break-words">{item.title}</h2>
          </div>
          <Since at={item.createdAt} />
        </header>
        <div className="flex min-w-0 flex-col gap-3 sm:pl-11">{children}</div>
      </article>
    </Card>
  )
}

/** Copies a command to the clipboard and says whether it worked. */
function CopyCommand({ command, label }: { command: string; label: string }) {
  return (
    <Button
      size="icon-sm"
      variant="ghost"
      aria-label={label}
      title={label}
      onClick={() =>
        void copyWithFallback(command)
          .then(() => toast.success(t('webPagesOverview.inboxCopied')))
          .catch(() => toast.error(t('webPagesOverview.inboxCopyFailed')))
      }
    >
      <Copy />
    </Button>
  )
}

function ApprovalItem({ item, highlight }: { item: AttentionItem; highlight: string | null }) {
  const namespace = item.approval?.namespace || 'exec'
  // Each card names its agent and copies its command (ApprovalCard).
  return (
    <ItemCard
      item={item}
      icon={<ShieldAlert />}
      kind={t('webPagesOverview.inboxKindApproval', { namespace })}
      tone="warn"
    >
      <ul role="list" className="flex flex-col gap-2">
        {item.members.map((member) =>
          member.approval ? (
            <ApprovalCard
              key={member.id}
              item={member.approval}
              highlighted={member.approval.id === highlight}
            />
          ) : null,
        )}
      </ul>
      <Link
        to="/settings/approvals"
        className="target inline-flex items-center self-start text-small text-accent underline-offset-2 hover:underline"
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
      tone="info"
    >
      {questions.map((q, qi) => (
        <fieldset key={`${qi}:${q.question}`} className="flex flex-col gap-1.5">
          <legend className="mb-1.5 font-medium">{q.question}</legend>
          <div className="flex flex-wrap gap-1.5">
            {q.options.map((opt) => {
              const on = picks[qi]?.includes(opt.label) ?? false
              return (
                <Button
                  key={opt.label}
                  size="sm"
                  variant="outline"
                  className={cn(
                    on && 'border-accent/50 bg-accent-tint text-accent hover:bg-accent-tint',
                  )}
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
        <Input
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
          className="sm:max-w-md"
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
              className="target inline-flex max-w-full items-center font-mono text-small text-accent underline-offset-2 hover:underline"
            >
              <span className="truncate">{key}</span>
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
      tone={error ? 'danger' : 'warn'}
    >
      {issue.detail ? <p className="text-text-muted break-words">{issue.detail}</p> : null}
      {issue.findingId ? (
        <code className="self-start rounded-sm bg-surface-2 px-1.5 py-0.5 font-mono text-small text-text-muted">
          {issue.findingId}
        </code>
      ) : null}
      {stepsOpen && issue.fixSteps.length > 0 ? (
        <ol
          aria-label={t('webPagesOverview.inboxFixSteps')}
          className="anim-enter flex flex-col gap-3 rounded-control border border-border bg-surface-2 p-3"
        >
          {issue.fixSteps.map((step, i) => (
            <li key={`${i}:${step.label ?? ''}`} className="flex gap-2">
              <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-surface-3 text-small font-medium tabular-nums">
                {formatNumber(i + 1)}
              </span>
              <div className="min-w-0 flex-1">
                {step.label ? <p className="font-medium">{step.label}</p> : null}
                {step.command ? (
                  <div className="mt-1 flex max-w-full items-center gap-1 rounded-control bg-surface-inset pl-3 shadow-inset">
                    <code
                      tabIndex={0}
                      className="block min-w-0 flex-1 overflow-x-auto py-1 font-mono text-small whitespace-pre"
                    >
                      {step.command}
                    </code>
                    <CopyCommand
                      command={step.command}
                      label={t('webPagesOverview.inboxCopyStep', { step: i + 1 })}
                    />
                  </div>
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
            className="target inline-flex items-center text-small text-accent underline-offset-2 hover:underline"
          >
            {t('webPagesOverview.inboxOpenHealth')}
          </Link>
        ) : null}
      </div>
    </ItemCard>
  )
}

/** A count that rises on arrival and on change. */
function Counted({ value }: { value: number }) {
  return <>{formatNumber(useCountUp(value))}</>
}

export function InboxPage() {
  const { items, issues } = useAttentionInbox()
  // A push notification opens /inbox?approval=<id>: that request is brought into view.
  const [params] = useSearchParams()
  const highlight = params.get('approval')
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
  const { entries, done } = usePresence(items, (item) => item.id)

  return (
    <div className={PAGE_CLASS}>
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

      <StatStrip
        label={t('webPagesOverview.inboxSummary')}
        items={(
          [
            ['approval', t('webPagesOverview.inboxApprovals'), <ShieldAlert />, 'warn'],
            ['question', t('webPagesOverview.inboxQuestions'), <MessageCircleQuestion />, 'info'],
            ['issue', t('webPagesOverview.inboxIssues'), <AlertTriangle />, 'danger'],
          ] as const
        ).map(([kind, label, icon, tone]) => ({
          label,
          icon,
          iconTone: count(kind) > 0 ? tone : 'neutral',
          value: <Counted value={count(kind)} />,
        }))}
      />

      {entries.length === 0 ? (
        <Card variant="plain" padding="none">
          <EmptyState
            className={live ? 'ov-caught-up' : undefined}
            icon={live ? <CheckCheck /> : <CloudOff />}
            title={
              live ? t('webPagesOverview.inboxEmpty') : t('webPagesOverview.inboxEmptyUnknown')
            }
            description={
              live
                ? t('webPagesOverview.inboxEmptyHint')
                : t('webPagesOverview.inboxEmptyUnknownHint')
            }
            action={
              <Button variant="outline" disabled={rechecking} onClick={() => void recheck()}>
                <RefreshCw className={rechecking ? 'animate-spin' : undefined} aria-hidden />
                {t('webPagesOverview.inboxRecheck')}
              </Button>
            }
          />
        </Card>
      ) : (
        <section aria-label={t('webPagesOverview.inboxList')} className="flex flex-col">
          {entries.map(({ item, leaving }, index) => (
            // One wrapper per item: it rises in, and when the item is
            // resolved it fades while the list closes up over it.
            <div
              key={item.id}
              className="ov-presence"
              data-leaving={leaving || undefined}
              aria-hidden={leaving || undefined}
              inert={leaving}
              onAnimationEnd={(e) => {
                if (leaving && e.target === e.currentTarget) done(item.id)
              }}
              style={staggerStyle(index)}
            >
              <div>
                <div className="pb-3">
                  {item.kind === 'approval' ? (
                    <ApprovalItem item={item} highlight={highlight} />
                  ) : item.kind === 'question' ? (
                    <QuestionItem item={item} answer={answer} />
                  ) : item.issue ? (
                    <IssueItem item={item} issue={item.issue} />
                  ) : null}
                </div>
              </div>
            </div>
          ))}
        </section>
      )}

      <footer className="flex flex-col gap-1 text-small text-text-muted">
        <p className="flex items-start gap-1.5">
          <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {t('webPagesOverview.inboxQuestionsGap')}
        </p>
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
