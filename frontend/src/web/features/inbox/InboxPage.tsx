import {
  AlertTriangle,
  ChevronDown,
  Clock,
  CloudOff,
  Copy,
  Info,
  MessageCircleQuestion,
  RefreshCw,
  ShieldAlert,
} from 'lucide-react'
import { useId, useState, type ReactNode } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { formatNumber, getLocale, t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesOverview'
import { composeAskAnswer, type AskQuestion } from '@/lib/ask-questions'
import type { AttentionItem, AttentionKind } from '@/lib/attention-inbox'
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
import { approvalCommand, approvalMonitor, useApprovals } from '@/services/approval-monitor'
import { Badge } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Input } from '@/web/components/ui/input'
import { PageHeader } from '@/web/components/ui/page-header'
import { StaleBadge } from '@/web/components/ui/states'
import { useCountUp } from '@/web/components/ui/use-count-up'
import { sendOnce } from '@/web/features/chat/send'
import {
  IconTile,
  PAGE_CLASS,
  staggerStyle,
  usePresence,
  type OverviewTone,
  type Presence,
} from '@/web/features/home/overview-ui'
import { ApprovalCard } from '@/web/shell/ApprovalTray'
import { sessionKeyToPath } from '@/web/shell/session-url'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import '@/web/styles/page-inbox.css'

// Inbox: everything waiting on the operator (useAttentionInbox), triaged by
// kind. Each group (Approvals, Questions, Issues) is one card of compact rows
// that expand in place; the first row of a group starts open, so the newest
// decision is one click away. Approvals with the same command are grouped and
// every occurrence keeps its own decisions; `ask_user` questions are answered
// by sending the composed answer as the session's next message; runtime
// issues carry their own actions. A resolved row folds away.

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
    <span className="hidden items-center gap-1 text-small whitespace-nowrap text-text-muted tabular-nums sm:inline-flex">
      <Clock className="size-3.5" aria-hidden />
      {t('webPagesOverview.inboxSince', { time })}
    </span>
  )
}

const GROUPS: readonly {
  kind: AttentionKind
  title: () => string
  hint: () => string
  icon: ReactNode
  tone: OverviewTone
}[] = [
  {
    kind: 'approval',
    title: () => t('webPagesOverview.inboxApprovals'),
    hint: () => t('webPagesOverview.inboxGroupApprovalsHint'),
    icon: <ShieldAlert />,
    tone: 'warn',
  },
  {
    kind: 'question',
    title: () => t('webPagesOverview.inboxQuestions'),
    hint: () => t('webPagesOverview.inboxGroupQuestionsHint'),
    icon: <MessageCircleQuestion />,
    tone: 'info',
  },
  {
    kind: 'issue',
    title: () => t('webPagesOverview.inboxIssues'),
    hint: () => t('webPagesOverview.inboxGroupIssuesHint'),
    icon: <AlertTriangle />,
    tone: 'danger',
  },
]

/**
 * One waiting item as a compact row: icon, title, a one-line preview and
 * when it started waiting. The row's head toggles its body, which holds the
 * decision. The body stays mounted while folded, so a half-written answer
 * survives folding.
 */
function ItemRow({
  item,
  icon,
  kind,
  tone,
  preview,
  previewMono = false,
  titleMono = false,
  open,
  onToggle,
  children,
}: {
  item: AttentionItem
  icon: ReactNode
  kind: string
  tone: OverviewTone
  preview?: string
  previewMono?: boolean
  titleMono?: boolean
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  const bodyId = useId()
  return (
    <article
      aria-label={t('webPagesOverview.inboxItemLabel', { kind, title: item.title })}
      data-kind={item.kind}
      data-ov-tone={tone}
      data-open={open || undefined}
      className="ib-row"
    >
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={onToggle}
        className="ib-row-head target flex w-full min-w-0 items-center gap-3 px-4 py-3 text-left sm:px-5"
      >
        <IconTile tone={tone}>{icon}</IconTile>
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-2">
            <span
              className={cn(
                'truncate font-medium',
                titleMono && 'font-mono text-[0.8125rem] tracking-tight',
              )}
            >
              {item.title}
            </span>
            <Badge tone={tone} className="hidden shrink-0 sm:inline-flex">
              {kind}
            </Badge>
            {item.sessions.length > 1 ? (
              <Badge className="shrink-0">
                {tPlural('webPagesOverview.inboxSeenIn', item.sessions.length)}
              </Badge>
            ) : null}
          </span>
          {preview ? (
            <span
              className={cn(
                'ib-preview mt-0.5 block truncate text-small text-text-muted',
                previewMono && 'font-mono',
              )}
            >
              {preview}
            </span>
          ) : null}
        </span>
        <Since at={item.createdAt} />
        <ChevronDown aria-hidden className="ib-chevron size-4 shrink-0 text-text-muted" />
      </button>
      <div id={bodyId} className="collapsible" data-open={open}>
        <div>
          <div className="flex min-w-0 flex-col gap-3 px-4 pb-4 sm:pr-5 sm:pl-[4.25rem]">
            {children}
          </div>
        </div>
      </div>
    </article>
  )
}

interface RowProps {
  item: AttentionItem
  open: boolean
  onToggle: () => void
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

function ApprovalItem({
  item,
  highlight,
  open,
  onToggle,
}: RowProps & { highlight: string | null }) {
  const namespace = item.approval?.namespace || 'exec'
  const command = item.approval ? approvalCommand(item.approval) : ''
  // Each occurrence names its agent and copies its command (ApprovalCard).
  return (
    <ItemRow
      item={item}
      icon={<ShieldAlert />}
      kind={t('webPagesOverview.inboxKindApproval', { namespace })}
      tone="warn"
      titleMono
      preview={command || item.detail}
      previewMono={Boolean(command)}
      open={open}
      onToggle={onToggle}
    >
      <ul role="list" className="flex flex-col divide-y divide-border">
        {item.members.map((member) =>
          member.approval ? (
            <ApprovalCard
              key={member.id}
              item={member.approval}
              highlighted={member.approval.id === highlight}
              className={cn(
                'rounded-control border-0 bg-transparent px-0 py-3 shadow-none first:pt-0 last:pb-0',
                member.approval.id === highlight && 'px-3 first:pt-3 last:pb-3',
              )}
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
    </ItemRow>
  )
}

function QuestionItem({ item, answer, open, onToggle }: RowProps & { answer: Gate }) {
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
    <ItemRow
      item={item}
      icon={<MessageCircleQuestion />}
      kind={t('webPagesOverview.inboxKindQuestion')}
      tone="info"
      preview={sessions.length === 1 ? sessions[0] : undefined}
      previewMono
      open={open}
      onToggle={onToggle}
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
                    'rounded-full',
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
    </ItemRow>
  )
}

function IssueItem({ item, issue, open, onToggle }: RowProps & { issue: RuntimeIssue }) {
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
        if (issue.fixSteps.length > 0) setStepsOpen((v) => !v)
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
    <ItemRow
      item={item}
      icon={<AlertTriangle />}
      kind={
        error ? t('webPagesOverview.inboxKindIssueError') : t('webPagesOverview.inboxKindIssueWarn')
      }
      tone={error ? 'danger' : 'warn'}
      preview={issue.detail}
      open={open}
      onToggle={onToggle}
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
          className="anim-enter flex flex-col gap-3 rounded-control border border-border bg-surface-inset p-3 shadow-inset"
        >
          {issue.fixSteps.map((step, i) => (
            <li key={`${i}:${step.label ?? ''}`} className="flex gap-2.5">
              <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-surface-3 text-small font-medium tabular-nums">
                {formatNumber(i + 1)}
              </span>
              <div className="min-w-0 flex-1">
                {step.label ? <p className="font-medium">{step.label}</p> : null}
                {step.command ? (
                  <div className="mt-1 flex max-w-full items-center gap-1 rounded-control border border-border bg-surface-1 pl-3">
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
    </ItemRow>
  )
}

/** A count that rises on arrival and on change. */
function Counted({ value }: { value: number }) {
  return <>{formatNumber(useCountUp(value))}</>
}

/**
 * "All caught up": a check that draws itself in a ring, on the canvas (no
 * box). Offline, the page cannot know, and says so with a dashed ring.
 */
function ZeroState({
  live,
  rechecking,
  onRecheck,
}: {
  live: boolean
  rechecking: boolean
  onRecheck: () => void
}) {
  return (
    <section
      aria-label={
        live ? t('webPagesOverview.inboxCaughtUp') : t('webPagesOverview.inboxEmptyUnknown')
      }
      className="anim-enter flex flex-col items-center gap-5 px-4 pt-10 pb-6 text-center sm:pt-16"
    >
      {live ? (
        <span aria-hidden className="ib-done">
          <svg viewBox="0 0 120 120">
            <circle className="ib-done-halo" cx="60" cy="60" r="56" />
            <circle className="ib-done-ring" cx="60" cy="60" r="44" pathLength={100} />
            <path className="ib-done-check" d="M42 61.5 L54.5 74 L79 48" pathLength={100} />
          </svg>
        </span>
      ) : (
        <span aria-hidden className="ib-done" data-offline>
          <svg viewBox="0 0 120 120">
            <circle className="ib-done-dashed" cx="60" cy="60" r="44" />
          </svg>
          <CloudOff className="ib-done-icon" />
        </span>
      )}
      <div className="flex max-w-md flex-col items-center gap-2">
        <h2 className="text-[1.75rem] leading-tight font-semibold tracking-[-0.025em]">
          {live ? t('webPagesOverview.inboxCaughtUp') : t('webPagesOverview.inboxEmptyUnknown')}
        </h2>
        <p className="text-pretty text-text-muted">
          {live
            ? t('webPagesOverview.inboxCaughtUpHint')
            : t('webPagesOverview.inboxEmptyUnknownHint')}
        </p>
      </div>
      {live ? (
        <ul
          role="list"
          aria-label={t('webPagesOverview.inboxEmptyHint')}
          className="flex flex-wrap justify-center gap-2"
        >
          {GROUPS.map((group) => (
            <li
              key={group.kind}
              className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface-1/60 py-1 pr-3 pl-2 text-small text-text-muted"
            >
              <span aria-hidden className="flex [&_svg]:size-3.5">
                {group.icon}
              </span>
              {group.title()}
              <span className="text-text tabular-nums">{formatNumber(0)}</span>
            </li>
          ))}
        </ul>
      ) : null}
      <Button variant="outline" disabled={rechecking} onClick={onRecheck}>
        <RefreshCw className={rechecking ? 'animate-spin' : undefined} aria-hidden />
        {t('webPagesOverview.inboxRecheck')}
      </Button>
    </section>
  )
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
  // Rows the operator opened or closed; the rest follow the default (the
  // first row of each group, and the row a notification points at, open).
  const [toggled, setToggled] = useState<Record<string, boolean>>({})
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

  const waiting = items.reduce((n, i) => n + i.members.length, 0)
  const hiddenIssues = issues.filter((i) => dismissed.includes(i.id)).length
  const { entries, done } = usePresence(items, (item) => item.id)
  const groups = GROUPS.map((group) => ({
    ...group,
    entries: entries.filter((e) => e.item.kind === group.kind),
  })).filter((group) => group.entries.length > 0)

  const isOpen = (entry: Presence<AttentionItem>, index: number): boolean => {
    const id = entry.item.id
    if (id in toggled) return toggled[id]!
    if (highlight && entry.item.members.some((m) => m.approval?.id === highlight)) return true
    return index === 0
  }

  const renderItem = (item: AttentionItem, open: boolean) => {
    const onToggle = () => setToggled((prev) => ({ ...prev, [item.id]: !open }))
    if (item.kind === 'approval')
      return <ApprovalItem item={item} highlight={highlight} open={open} onToggle={onToggle} />
    if (item.kind === 'question')
      return <QuestionItem item={item} answer={answer} open={open} onToggle={onToggle} />
    if (item.issue)
      return <IssueItem item={item} issue={item.issue} open={open} onToggle={onToggle} />
    return null
  }

  return (
    <div className={PAGE_CLASS}>
      <PageHeader
        title={t('webPagesOverview.inboxTitle')}
        badge={
          !live && everConnected ? (
            <StaleBadge />
          ) : waiting > 0 ? (
            <Badge tone="warn" dot className="anim-fade-in">
              {tPlural('webPagesOverview.inboxWaiting', waiting)}
            </Badge>
          ) : null
        }
        description={t('webPagesOverview.inboxSubtitle')}
        actions={
          entries.length > 0 ? (
            <Button variant="outline" disabled={rechecking} onClick={() => void recheck()}>
              <RefreshCw className={rechecking ? 'animate-spin' : undefined} aria-hidden />
              {t('webPagesOverview.inboxRecheck')}
            </Button>
          ) : null
        }
      />

      {entries.length === 0 ? (
        <ZeroState live={live} rechecking={rechecking} onRecheck={() => void recheck()} />
      ) : (
        <section aria-label={t('webPagesOverview.inboxList')} className="flex flex-col gap-7">
          {groups.map((group, gi) => {
            const count = group.entries
              .filter((e) => !e.leaving)
              .reduce((n, e) => n + e.item.members.length, 0)
            let shown = 0
            return (
              <section
                key={group.kind}
                aria-labelledby={`inbox-group-${group.kind}`}
                className="ib-rise flex flex-col gap-2.5"
                style={staggerStyle(gi)}
                data-ov-tone={group.tone}
              >
                <header className="flex min-w-0 items-center gap-2.5 px-1">
                  <span aria-hidden className="ib-group-dot" />
                  <h2
                    id={`inbox-group-${group.kind}`}
                    className="flex items-center gap-2 text-section"
                  >
                    {group.title()}
                    <span className="ib-count tabular-nums">
                      <Counted value={count} />
                    </span>
                  </h2>
                  <p className="ml-auto hidden truncate text-small text-text-muted sm:block">
                    {group.hint()}
                  </p>
                </header>
                <div className="ib-list">
                  {group.entries.map((entry) => {
                    const { item, leaving } = entry
                    const index = leaving ? -1 : shown++
                    return (
                      // One wrapper per row: it rises in, and when the item
                      // is resolved it fades while the list closes up over it.
                      <div
                        key={item.id}
                        className="ib-presence"
                        data-leaving={leaving || undefined}
                        aria-hidden={leaving || undefined}
                        inert={leaving}
                        onAnimationEnd={(e) => {
                          if (leaving && e.target === e.currentTarget) done(item.id)
                        }}
                        style={staggerStyle(gi + Math.max(index, 0))}
                      >
                        <div>{renderItem(item, !leaving && isOpen(entry, index))}</div>
                      </div>
                    )
                  })}
                </div>
              </section>
            )
          })}
        </section>
      )}

      <footer className="mt-auto flex flex-col gap-1 pt-2 text-small text-text-muted">
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
