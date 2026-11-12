import {
  Bot,
  CheckCircle2,
  Clock,
  Download,
  FileText,
  HelpCircle,
  ListChecks,
  PanelRightOpen,
  RotateCcw,
  Route,
  Scissors,
  TriangleAlert,
  Undo2,
} from 'lucide-react'
import { lazy, Suspense, useState } from 'react'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { composeAskAnswer, parseAskQuestions, type AskQuestion } from '@/lib/ask-questions'
import {
  artifactDownloadUrl,
  artifactKind,
  artifactKindLabel,
  artifactName,
  artifactSizeLabel,
  isImageArtifact,
} from '@/views/chat/transcript/artifacts'
import { modelDisplayName } from '@/views/chat/transcript/routerFx'
import { cn } from '@/lib/utils'
import { Button, buttonVariants } from '@/web/components/ui/button'
import { Markdown } from '../markdown'
import type { Item } from '../model/types'
import { useChatActions } from '../transcript/context'

const DataCard = lazy(async () => ({ default: (await import('./DataCards')).DataCard }))
const TradingCard = lazy(async () => ({ default: (await import('./TradingCards')).TradingCard }))

const TRADING = new Set(['dca', 'lp', 'markets', 'trigger'])

/** The surface every transcript card shares: a raised card, icon in a tile. */
export const CARD = 'chat-card'
export const CARD_PLACEHOLDER = 'chat-card skeleton-shimmer h-28'

export function CardHeader({
  icon,
  title,
  tone,
  children,
}: {
  icon: React.ReactNode
  title: React.ReactNode
  /** `accent`: the card asks for a decision (plan, question). */
  tone?: 'accent'
  children?: React.ReactNode
}) {
  return (
    <header className="chat-card-header">
      <span className="chat-tile" data-tone={tone} aria-hidden>
        {icon}
      </span>
      <h3 className="min-w-0 flex-1 truncate">{title}</h3>
      {children}
    </header>
  )
}

function CardShell({
  icon,
  title,
  tone,
  children,
}: {
  icon: React.ReactNode
  title: React.ReactNode
  tone?: 'accent'
  children?: React.ReactNode
}) {
  return (
    <section className={cn(CARD, 'text-[length:var(--text-prose)]')}>
      <CardHeader icon={icon} title={title} tone={tone} />
      {children}
    </section>
  )
}

export function PlanCard({ plan, locked }: { plan: string; locked: boolean }) {
  const { approvePlan } = useChatActions()
  const [done, setDone] = useState(false)
  return (
    <CardShell icon={<ListChecks />} title={t('webChat.planTitle')} tone="accent">
      <Markdown text={plan} />
      <div className="chat-card-footer">
        {done || locked ? (
          <span className="anim-fade-in inline-flex items-center gap-1.5 text-small text-text-muted">
            <CheckCircle2 className="size-4 text-ok" aria-hidden data-status-icon />
            {t('webChat.planApproved')}
          </span>
        ) : (
          <Button
            variant="primary"
            size="sm"
            onClick={() => {
              setDone(true)
              void approvePlan()
            }}
          >
            {t('webChat.planApprove')}
          </Button>
        )}
      </div>
    </CardShell>
  )
}

export function AskCard({ payload, answered }: { payload: unknown; answered: boolean }) {
  const { answer } = useChatActions()
  const questions: AskQuestion[] = parseAskQuestions({ name: 'ask_user', result: payload }) ?? []
  const [picks, setPicks] = useState<string[][]>(() => questions.map(() => []))
  const [sent, setSent] = useState(false)
  if (questions.length === 0) return null
  const locked = answered || sent

  const toggle = (qi: number, label: string, multi: boolean) =>
    setPicks((prev) =>
      prev.map((list, i) =>
        i !== qi
          ? list
          : multi
            ? list.includes(label)
              ? list.filter((l) => l !== label)
              : [...list, label]
            : [label],
      ),
    )

  const submit = async () => {
    const text = composeAskAnswer(questions, picks)
    if (!text.trim()) return
    setSent(true)
    if (!(await answer(text))) setSent(false)
  }

  return (
    <CardShell icon={<HelpCircle />} title={t('webChat.askTitle')} tone="accent">
      <div className="flex flex-col gap-3">
        {questions.map((q, qi) => (
          <fieldset key={qi} disabled={locked} className="flex flex-col gap-1.5">
            <legend className="mb-1.5 font-medium">{q.question}</legend>
            {q.options.map((opt) => {
              const checked = picks[qi]?.includes(opt.label) ?? false
              return (
                <label key={opt.label} className="chat-option">
                  <input
                    type={q.multiSelect ? 'checkbox' : 'radio'}
                    name={`ask-${qi}`}
                    checked={checked}
                    onChange={() => toggle(qi, opt.label, q.multiSelect)}
                    className="mt-1"
                  />
                  <span>
                    {opt.label}
                    {opt.description ? (
                      <span className="block text-small text-text-muted">{opt.description}</span>
                    ) : null}
                  </span>
                </label>
              )
            })}
          </fieldset>
        ))}
        {locked ? (
          <span className="anim-fade-in inline-flex items-center gap-1.5 text-small text-text-muted">
            <CheckCircle2 className="size-4 text-ok" aria-hidden data-status-icon />
            {t('webChat.askAnswered')}
          </span>
        ) : (
          <div>
            <Button
              size="sm"
              variant="primary"
              onClick={() => void submit()}
              disabled={picks.every((p) => p.length === 0)}
            >
              {t('webChat.askSubmit')}
            </Button>
          </div>
        )}
      </div>
    </CardShell>
  )
}

export function ArtifactChip({ item }: { item: Extract<Item, { kind: 'artifact' }> }) {
  const { openPanel } = useChatActions()
  const artifact = item.artifact
  const name = artifactName(artifact)
  const kind = artifactKind(artifact)
  const url = artifactDownloadUrl(artifact)
  return (
    <div className="chat-card chat-artifact lift">
      {isImageArtifact(artifact) && url ? (
        <img src={url} alt="" className="chat-artifact-thumb object-cover" data-decorative />
      ) : (
        <span className="chat-artifact-thumb chat-tile" aria-hidden>
          <FileText />
        </span>
      )}
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium">{name}</p>
        <p className="text-small text-text-muted tabular-nums">
          {artifactKindLabel(kind)} · {artifactSizeLabel(artifact.size)}
        </p>
      </div>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={t('webChat.artifactOpen', { name })}
        onClick={(e) => openPanel({ kind: 'artifact', id: item.id, title: name }, e.currentTarget)}
      >
        <PanelRightOpen />
      </Button>
      {url ? (
        <a
          href={url}
          download={name}
          aria-label={t('webChat.artifactDownload', { name })}
          className={buttonVariants({ size: 'icon-sm', variant: 'ghost' })}
        >
          <Download aria-hidden />
        </a>
      ) : null}
    </div>
  )
}

export function AcpCard({ args, result }: { args: unknown; result: unknown }) {
  const rpc = useRpc()
  const spec = (args ?? {}) as Record<string, unknown>
  let parsed: Record<string, unknown> = {}
  try {
    parsed = (typeof result === 'string' ? JSON.parse(result) : result) as Record<string, unknown>
  } catch {
    parsed = {}
  }
  const key = String(parsed?.session_key ?? parsed?.child_session_key ?? '')
  const name = String(spec.agent_id ?? spec.agentId ?? spec.harness ?? 'harness')
  const [cancelled, setCancelled] = useState(false)
  return (
    <CardShell icon={<Bot />} title={t('webChat.acpTitle', { name })}>
      {key ? <p className="chat-id-chip w-fit max-w-full truncate">{key}</p> : null}
      {key ? (
        <div className="chat-card-footer">
          <Button
            size="sm"
            variant="outline"
            disabled={cancelled}
            onClick={() => {
              setCancelled(true)
              void rpc.call('acp.cancel', { session_key: key }).catch(() => setCancelled(false))
            }}
          >
            {cancelled ? t('webChat.acpCancelled') : t('webChat.acpCancel')}
          </Button>
        </div>
      ) : null}
    </CardShell>
  )
}

/** Renders any non-text, non-tool item of a turn. */
export function CardItem({ item, answered }: { item: Item; answered: boolean }) {
  switch (item.kind) {
    case 'plan':
      return <PlanCard plan={item.plan} locked={answered} />
    case 'ask':
      return <AskCard payload={item.payload} answered={answered} />
    case 'acp':
      return <AcpCard args={item.args} result={item.result} />
    case 'artifact':
      return <ArtifactChip item={item} />
    case 'card':
      return (
        <Suspense fallback={<div className={CARD_PLACEHOLDER} aria-hidden />}>
          {TRADING.has(item.category) ? (
            <TradingCard category={item.category} artifact={item.artifact} />
          ) : (
            <DataCard category={item.category} artifact={item.artifact} />
          )}
        </Suspense>
      )
    case 'compaction':
      return (
        <div
          role="separator"
          aria-label={t('webChat.compacted')}
          className="flex items-center gap-3 py-1 text-label text-text-muted"
        >
          <span className="chat-rule" />
          <Scissors className="size-3.5" aria-hidden />
          {t('webChat.compacted')}
          <span className="chat-rule -scale-x-100" />
        </div>
      )
    case 'routerDecision':
      return (
        <p className="flex items-center gap-1.5 text-small text-text-muted">
          <Route className="size-3.5" aria-hidden />
          {t('webChat.routerDecision', { model: modelDisplayName(item.model) })}
          {item.tier ? (
            <span className="rounded-md border border-border bg-surface-2 px-1.5 font-mono text-small">
              {item.tier}
            </span>
          ) : null}
        </p>
      )
    case 'steering':
      return (
        <p className="chat-notice">
          <Undo2 aria-hidden />
          {t('webChat.steering', { text: item.text })}
        </p>
      )
    case 'restartRecovery':
      return (
        <p data-role="notice" className="chat-notice">
          <RotateCcw aria-hidden />
          {t('webChat.restartRecovery')}
        </p>
      )
    case 'subagentCompletion': {
      const status = String(item.payload.status ?? '')
      const summary = String(item.payload.result ?? item.payload.terminal_reason ?? '')
      return (
        <CardShell icon={<Bot />} title={t('webChat.subagentTitle', { status })}>
          {summary ? <Markdown text={summary} /> : null}
        </CardShell>
      )
    }
    case 'cronResult':
      return (
        <CardShell
          icon={<Clock />}
          title={t('webChat.cronResult', { summary: '' }).replace(/:\s*$/, '')}
        >
          <Markdown text={item.text} />
        </CardShell>
      )
    case 'warning':
    case 'error':
      return (
        <p
          role="alert"
          className="chat-notice"
          data-tone={item.kind === 'error' ? 'danger' : 'warn'}
        >
          <TriangleAlert aria-hidden data-status-icon />
          {item.kind === 'error'
            ? t('webChat.errorItem', { text: item.text })
            : t('webChat.warning', { text: item.text })}
        </p>
      )
    case 'note':
      // Local output (`/model`): plain text, shown as written.
      return (
        <pre data-role="note" className="chat-note">
          {item.text}
        </pre>
      )
    default:
      return null
  }
}
