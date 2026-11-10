import {
  Bot,
  CheckCircle2,
  Clock,
  Download,
  FileText,
  HelpCircle,
  ListChecks,
  PanelRightOpen,
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
import { Button, buttonVariants } from '@/web/components/ui/button'
import { Markdown } from '../markdown'
import type { Item } from '../model/types'
import { useChatActions } from '../transcript/context'

const DataCard = lazy(async () => ({ default: (await import('./DataCards')).DataCard }))
const TradingCard = lazy(async () => ({ default: (await import('./TradingCards')).TradingCard }))

const TRADING = new Set(['dca', 'lp', 'markets', 'trigger'])

function CardShell({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode
  title: React.ReactNode
  children?: React.ReactNode
}) {
  return (
    <section className="rounded-lg border border-border bg-surface-1 p-3 text-[length:var(--text-prose)]">
      <header className="mb-2 flex items-center gap-2 text-ui font-medium">
        <span className="text-text-muted [&_svg]:size-4" aria-hidden>
          {icon}
        </span>
        <h3 className="min-w-0 flex-1 truncate">{title}</h3>
      </header>
      {children}
    </section>
  )
}

export function PlanCard({ plan, locked }: { plan: string; locked: boolean }) {
  const { approvePlan } = useChatActions()
  const [done, setDone] = useState(false)
  return (
    <CardShell icon={<ListChecks />} title={t('webChat.planTitle')}>
      <Markdown text={plan} />
      <div className="mt-3">
        {done || locked ? (
          <span className="inline-flex items-center gap-1 text-small text-text-muted">
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
    <CardShell icon={<HelpCircle />} title={t('webChat.askTitle')}>
      <div className="flex flex-col gap-3">
        {questions.map((q, qi) => (
          <fieldset key={qi} disabled={locked} className="flex flex-col gap-1.5">
            <legend className="mb-1 font-medium">{q.question}</legend>
            {q.options.map((opt) => {
              const checked = picks[qi]?.includes(opt.label) ?? false
              return (
                <label
                  key={opt.label}
                  className="flex cursor-pointer items-start gap-2 rounded-md px-2 py-1 hover:bg-surface-2"
                >
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
          <span className="inline-flex items-center gap-1 text-small text-text-muted">
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
    <div className="flex items-center gap-3 rounded-lg border border-border bg-surface-1 px-3 py-2">
      {isImageArtifact(artifact) && url ? (
        <img src={url} alt="" className="size-10 rounded object-cover" data-decorative />
      ) : (
        <FileText className="size-5 text-text-muted" aria-hidden />
      )}
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium">{name}</p>
        <p className="text-small text-text-muted">
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
      {key ? <p className="font-mono text-small text-text-muted">{key}</p> : null}
      {key ? (
        <div className="mt-2 flex gap-2">
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
        <Suspense
          fallback={
            <div className="h-24 rounded-lg border border-border bg-surface-1" aria-hidden />
          }
        >
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
          className="flex items-center gap-3 py-2 text-small text-text-muted"
        >
          <span className="h-px flex-1 bg-border" />
          <Scissors className="size-3.5" aria-hidden />
          {t('webChat.compacted')}
          <span className="h-px flex-1 bg-border" />
        </div>
      )
    case 'routerDecision':
      return (
        <p className="flex items-center gap-1.5 text-small text-text-muted">
          <Route className="size-3.5" aria-hidden />
          {t('webChat.routerDecision', { model: modelDisplayName(item.model) })}
          {item.tier ? <span className="rounded bg-surface-3 px-1">{item.tier}</span> : null}
        </p>
      )
    case 'steering':
      return (
        <p className="flex items-center gap-1.5 text-small text-text-muted">
          <Undo2 className="size-3.5" aria-hidden />
          {t('webChat.steering', { text: item.text })}
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
        <p role="alert" className="flex items-center gap-1.5 text-danger">
          <TriangleAlert className="size-4" aria-hidden data-status-icon />
          {item.kind === 'error'
            ? t('webChat.errorItem', { text: item.text })
            : t('webChat.warning', { text: item.text })}
        </p>
      )
    case 'note':
      // Local output (`/model`): plain text, shown as written.
      return (
        <pre
          data-role="note"
          className="rounded-lg border border-border bg-surface-1 px-3 py-2 font-sans text-small whitespace-pre-wrap text-text-muted"
        >
          {item.text}
        </pre>
      )
    default:
      return null
  }
}
