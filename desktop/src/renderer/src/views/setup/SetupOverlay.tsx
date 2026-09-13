import { Check, ChevronDown, ChevronUp, LoaderCircle, Minus, X } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { useConnection } from '@/stores/connection'
import { bootstrapProgress, type BootstrapState, type StageProgress } from '@shared/bootstrap'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useBootstrap } from '~/stores/bootstrap'
import { useGateway } from '~/stores/gateway'
import agentosMark from '@/assets/agentos-mark.png'
import { ProviderStep } from './ProviderStep'
import './setup.css'

const INSTALL_ONE_LINER =
  'curl -fsSL https://raw.githubusercontent.com/use-agent-os/agent-os/main/install.sh | bash'

type Step = 1 | 2 | 3

/**
 * The first-run flow, full window: nothing else in the app is usable
 * without an engine. One stage, three numbered steps across the top
 * (Install → Provider → Ready) so the person always knows how much is
 * left, and one screen per state underneath: welcome, progress, failure,
 * then the provider step, which ends on "all set".
 */
export function SetupOverlay() {
  const state = useBootstrap((s) => s.state)
  const dismissed = useBootstrap((s) => s.dismissed)
  if (dismissed) return null
  if (state.phase === 'ready' || state.phase === 'checking') return null
  const step: Step = state.phase === 'succeeded' ? 2 : 1
  return (
    <div className="setup__overlay" role="dialog" aria-modal="true" aria-label={t('setup.brand')}>
      <Stage step={step} wide={state.phase === 'succeeded'} phase={state.phase}>
        {state.phase === 'choice' ? (
          <Welcome state={state} />
        ) : state.phase === 'running' ? (
          <Progress state={state} />
        ) : state.phase === 'succeeded' ? (
          <Done />
        ) : (
          <Failure state={state} />
        )}
      </Stage>
    </div>
  )
}

function Stage({
  step,
  wide,
  phase,
  children,
}: {
  step: Step
  wide: boolean
  phase: string
  children: ReactNode
}) {
  const steps: { n: Step; label: string }[] = [
    { n: 1, label: t('setup.step.install') },
    { n: 2, label: t('setup.step.provider') },
    { n: 3, label: t('setup.step.ready') },
  ]
  return (
    <div className="setup" data-phase={phase} data-wide={wide ? 'true' : undefined}>
      <header className="setup__head">
        <div className="setup__brand">
          <img src={agentosMark} alt="" draggable={false} />
          <span>{t('setup.brand')}</span>
          <span className="setup__brand-sub">{t('setup.brand.platform')}</span>
        </div>
        <ol className="setup__rail" aria-label="Setup steps">
          {steps.map((s) => (
            <li
              key={s.n}
              data-state={s.n < step ? 'done' : s.n === step ? 'current' : 'todo'}
              aria-current={s.n === step ? 'step' : undefined}
            >
              <span className="setup__rail-n" aria-hidden>
                {s.n < step ? <Check className="size-3" strokeWidth={3} /> : s.n}
              </span>
              {s.label}
            </li>
          ))}
        </ol>
      </header>
      <div className="setup__body">{children}</div>
    </div>
  )
}

function Welcome({ state }: { state: BootstrapState }) {
  const install = useBootstrap((s) => s.install)
  const connectExisting = useBootstrap((s) => s.connectExisting)
  const update = state.mode === 'update'
  const d = state.discovery
  return (
    <div className="setup__welcome">
      <div className="setup__hero" aria-hidden>
        <img src={agentosMark} alt="" draggable={false} />
      </div>
      <h1 className="setup__title setup__title--big">
        {update ? t('setup.update.title') : t('setup.install.title')}
      </h1>
      <p className="setup__blurb">{update ? t('setup.update.blurb') : t('setup.install.blurb')}</p>
      {d ? (
        <div className="setup__chips">
          {d.version ? (
            <span className="setup__chip">
              {t('setup.found')} <code>{d.version}</code>
            </span>
          ) : null}
          <span className="setup__chip">
            {t('setup.ships')} <code>{d.appVersion}</code>
          </span>
        </div>
      ) : null}
      <div className="setup__actions setup__actions--center">
        <Button variant="primary" className="setup__primary" onClick={() => void install()}>
          {update ? t('setup.update') : t('setup.install')}
        </Button>
      </div>
      <button type="button" className="setup__link" onClick={() => void connectExisting()}>
        {t('setup.connectExisting')}
        <span>{t('setup.connectExisting.help')}</span>
      </button>
    </div>
  )
}

function Progress({ state }: { state: BootstrapState }) {
  const cancel = useBootstrap((s) => s.cancel)
  const [details, setDetails] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const done = state.stages.filter((s) => s.state !== 'pending' && s.state !== 'running').length
  const percent = Math.round(bootstrapProgress(state.stages) * 100)
  return (
    <>
      <h1 className="setup__title">
        {state.mode === 'update' ? t('setup.running.update') : t('setup.running.install')}
      </h1>
      <p className="setup__blurb">{t('setup.running.blurb')}</p>
      <div
        className="setup__bar"
        role="progressbar"
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <span style={{ width: `${percent}%` }} />
      </div>
      <div className="setup__count">
        {done} / {state.stages.length} {t('setup.steps')} · {percent}%
      </div>
      <StageList stages={state.stages} />
      <div className="setup__foot">
        <button type="button" className="setup__toggle" onClick={() => setDetails((v) => !v)}>
          {details ? (
            <ChevronUp className="size-3.5" aria-hidden />
          ) : (
            <ChevronDown className="size-3.5" aria-hidden />
          )}
          {details ? t('setup.details.hide') : t('setup.details.show')}
        </button>
        <Button
          disabled={cancelling}
          onClick={() => {
            setCancelling(true)
            void cancel()
          }}
        >
          {cancelling ? t('setup.cancelling') : t('setup.cancel')}
        </Button>
      </div>
      {details ? <LogPanel state={state} /> : null}
    </>
  )
}

function Failure({ state }: { state: BootstrapState }) {
  const install = useBootstrap((s) => s.install)
  const connectExisting = useBootstrap((s) => s.connectExisting)
  const openLog = useBootstrap((s) => s.openLog)
  const cancelled = state.phase === 'cancelled'

  const copy = async () => {
    const text = [state.error ?? '', '', ...state.log.map((l) => `[${l.stream}] ${l.line}`)].join(
      '\n',
    )
    await navigator.clipboard?.writeText(text)
    toast.success(t('setup.copied'), { id: 'setup-copy' })
  }

  return (
    <>
      <h1 className="setup__title">
        {cancelled ? t('setup.cancelled.title') : t('setup.failed.title')}
      </h1>
      <p className="setup__blurb">{t('setup.failed.blurb')}</p>
      {state.error && !cancelled ? (
        <pre className="setup__error" data-testid="setup-error">
          {state.error}
        </pre>
      ) : null}
      <StageList stages={state.stages} />
      <div className="setup__actions">
        <Button variant="primary" className="setup__primary" onClick={() => void install()}>
          {t('setup.retry')}
        </Button>
        <Button onClick={() => void copy()}>{t('setup.copyOutput')}</Button>
        {state.logPath ? (
          <Button onClick={() => void openLog()}>{t('setup.openLog')}</Button>
        ) : null}
      </div>
      <div className="setup__manual">
        <span>{t('setup.manual')}</span>
        <code>{INSTALL_ONE_LINER}</code>
      </div>
      <button type="button" className="setup__link" onClick={() => void connectExisting()}>
        {t('setup.connectExisting')}
        <span>{t('setup.connectExisting.help')}</span>
      </button>
      {/* The failure screen opens the output by itself: the reason is in there. */}
      <LogPanel state={state} />
    </>
  )
}

/**
 * After the install: wait for the gateway, then hand over to the provider
 * step. The install script never asks questions; the app owns this one.
 */
function Done() {
  const dismiss = useBootstrap((s) => s.dismiss)
  const gateway = useGateway((s) => s.status.state)
  const connected = useConnection((s) => s.state === 'connected')
  const [waitedLong, setWaitedLong] = useState(false)
  useEffect(() => {
    const timer = window.setTimeout(() => setWaitedLong(true), 60_000)
    return () => window.clearTimeout(timer)
  }, [])

  if (!connected) {
    return (
      <>
        <h1 className="setup__title">{t('setup.done.title')}</h1>
        <p className="setup__blurb setup__blurb--row">
          <LoaderCircle className="setup__spin size-4" aria-hidden />
          {gateway === 'error' || waitedLong ? t('setup.done.waiting') : t('setup.done.starting')}
        </p>
        {gateway === 'error' || waitedLong ? (
          <div className="setup__actions">
            <Button onClick={dismiss}>{t('setup.done.continue')}</Button>
          </div>
        ) : null}
      </>
    )
  }
  return <ProviderStep onDone={dismiss} />
}

function StageList({ stages }: { stages: StageProgress[] }) {
  const now = useTicking(stages.some((s) => s.state === 'running'))
  if (stages.length === 0) return null
  return (
    <ol className="setup__stages">
      {stages.map((s) => (
        <li key={s.name} data-state={s.state}>
          <span className="setup__stage-mark" aria-hidden>
            {s.state === 'running' ? (
              <LoaderCircle className="setup__spin size-3.5" />
            ) : s.state === 'succeeded' ? (
              <Check className="size-3.5" />
            ) : s.state === 'skipped' ? (
              <Minus className="size-3.5" />
            ) : s.state === 'failed' ? (
              <X className="size-3.5" />
            ) : null}
          </span>
          <span className="setup__stage-title">{s.title}</span>
          <span className="setup__stage-time">
            {s.state === 'running' && s.startedAt !== null
              ? formatElapsed(now - s.startedAt)
              : s.durationMs !== null
                ? formatElapsed(s.durationMs)
                : ''}
          </span>
        </li>
      ))}
    </ol>
  )
}

function LogPanel({ state }: { state: BootstrapState }) {
  const ref = useRef<HTMLPreElement>(null)
  useEffect(() => {
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
  }, [state.log.length])
  return (
    <section className="setup__log" aria-label={t('setup.output')}>
      <header>
        <span>{t('setup.output')}</span>
        <span>
          {state.log.length} {t('setup.lines')}
        </span>
      </header>
      <pre ref={ref} data-testid="setup-log">
        {state.log.map((l, i) => (
          <span key={i} data-stream={l.stream}>
            {l.line}
            {'\n'}
          </span>
        ))}
      </pre>
    </section>
  )
}

/** A 1 Hz clock, only while something is running, so a long step reads as working. */
function useTicking(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])
  return now
}

export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return `${m}m ${String(s % 60).padStart(2, '0')}s`
}
