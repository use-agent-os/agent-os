import {
  ArrowLeft,
  ArrowRight,
  Check,
  CircleDot,
  ListChecks,
  MessageSquare,
  PartyPopper,
} from 'lucide-react'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webSetConfig'
import { cn } from '@/lib/utils'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import {
  audioStatusText,
  configCliArg,
  finishEnvRecoveryCommands,
  finishSummary,
  imageGenerationStatusText,
  initialStepFromStatus,
  memoryEmbeddingStatusText,
  onboardingReasons,
  searchStatusText,
} from '@/views/setup/logic'
import { Button, buttonVariants } from '@/web/components/ui/button'
import {
  audioAdapter,
  imageAdapter,
  memoryEmbeddingAdapter,
  providerAdapter,
  searchAdapter,
} from './onboarding-adapters'
import { SettingsCard } from '../ui'
import { CommandLine, RouterForm, SnapshotGate, SpecForm } from './onboarding-forms'

type Step = 'provider' | 'router' | 'extras' | 'finish'
const ORDER: Step[] = ['provider', 'router', 'extras', 'finish']

function stepLabel(step: Step): string {
  switch (step) {
    case 'provider':
      return t('webSetConfig.stepProvider')
    case 'router':
      return t('webSetConfig.stepRouter')
    case 'extras':
      return t('webSetConfig.stepExtras')
    default:
      return t('webSetConfig.stepFinish')
  }
}

function initialStep(snapshot: SettingsSnapshot, requested: string | null): Step {
  if (requested && ORDER.includes(requested as Step)) return requested as Step
  const fromStatus = initialStepFromStatus(snapshot.status ?? {})
  return fromStatus === 'channels' ? 'finish' : (fromStatus as Step)
}

function stepHelp(step: Step): string {
  switch (step) {
    case 'provider':
      return t('webSetConfig.stepProviderHelp')
    case 'router':
      return t('webSetConfig.stepRouterHelp')
    case 'extras':
      return t('webSetConfig.stepExtrasHelp')
    default:
      return t('webSetConfig.stepFinishHelp')
  }
}

function Stepper({ snapshot }: { snapshot: SettingsSnapshot }) {
  const [params] = useSearchParams()
  const [step, setStep] = useState<Step>(() => initialStep(snapshot, params.get('step')))
  const index = ORDER.indexOf(step)
  const status = snapshot.status ?? {}
  const config = snapshot.config ?? {}
  const summary = finishSummary(status, config)
  const reasons = onboardingReasons(status, config)
  const commands = finishEnvRecoveryCommands(status)
  const configArg = configCliArg(snapshot.configPath)

  return (
    <div className="flex flex-col gap-5">
      <div className="set-card flex flex-col gap-4">
        <ol
          aria-label={t('webSetConfig.stepOf', {
            current: index + 1,
            total: ORDER.length,
            name: stepLabel(step),
          })}
          className="set-stepper"
        >
          {ORDER.map((s, i) => (
            <li key={s} data-done={i < index || undefined}>
              <button
                type="button"
                aria-current={s === step ? 'step' : undefined}
                onClick={() => setStep(s)}
                className={cn(
                  'press target relative flex items-center gap-2 rounded-full py-1 pr-2.5 pl-1 text-ui hover:bg-surface-3',
                  s === step ? 'font-medium text-text' : 'text-text-muted',
                )}
              >
                <span className="set-stepper-dot" aria-hidden>
                  {i < index ? <Check /> : i + 1}
                </span>
                <span className="set-stepper-label">{stepLabel(s)}</span>
              </button>
            </li>
          ))}
        </ol>
        <div key={`${step}-head`} className="anim-fade-in border-t border-border pt-3.5">
          <p className="text-label text-text-muted">
            {t('webSetConfig.stepCount', { current: index + 1, total: ORDER.length })}
          </p>
          <h2 className="set-card-title mt-0.5">{stepLabel(step)}</h2>
          <p className="mt-0.5 text-small text-pretty text-text-muted">{stepHelp(step)}</p>
        </div>
      </div>

      <div key={`${step}-body`} className="anim-enter flex flex-col gap-4">
        {step === 'provider' ? (
          <SpecForm
            title={t('webSetConfig.provider')}
            adapter={providerAdapter}
            snapshot={snapshot}
            recovery="llm"
          />
        ) : null}
        {step === 'router' ? <RouterForm snapshot={snapshot} /> : null}
        {step === 'extras' ? (
          <>
            <SpecForm
              title={t('webSetConfig.webSearch')}
              adapter={searchAdapter}
              snapshot={snapshot}
              status={searchStatusText(status, config)}
              recovery="search"
            />
            <SpecForm
              title={t('webSetConfig.embedding')}
              adapter={memoryEmbeddingAdapter}
              snapshot={snapshot}
              status={memoryEmbeddingStatusText(status, config)}
              recovery="memory_embedding"
            />
            <SpecForm
              title={t('webSetConfig.image')}
              adapter={imageAdapter}
              snapshot={snapshot}
              status={imageGenerationStatusText(status)}
              recovery="image_generation"
            />
            <SpecForm
              title={t('webSetConfig.audio')}
              adapter={audioAdapter}
              snapshot={snapshot}
              status={audioStatusText(status)}
              recovery="audio"
            />
          </>
        ) : null}
        {step === 'finish' ? (
          <SettingsCard
            aria-label={t('webSetConfig.finishTitle')}
            title={t('webSetConfig.finishTitle')}
            icon={reasons.length ? <ListChecks /> : <PartyPopper />}
            className="relative overflow-hidden"
          >
            {reasons.length ? null : (
              <div
                aria-hidden
                className="pointer-events-none absolute inset-0 bg-[radial-gradient(70%_100%_at_100%_0%,var(--accent-tint),transparent_70%)]"
              />
            )}
            <dl className="set-list relative mt-3 shadow-none">
              {Object.entries(summary).map(([k, v]) => (
                <div
                  key={k}
                  className="grid grid-cols-[minmax(0,10rem)_minmax(0,1fr)] gap-x-4 px-3.5 py-2"
                >
                  <dt className="text-small text-text-muted">{k}</dt>
                  <dd className="min-w-0 text-small break-words">{v || '—'}</dd>
                </div>
              ))}
            </dl>
            {reasons.length ? (
              <div className="relative mt-4">
                <p className="text-label text-text-muted">{t('webSetConfig.finishActions')}</p>
                <ul className="mt-1.5 flex flex-col gap-1.5">
                  {reasons.map((r, i) => (
                    <li key={i} className="flex items-start gap-2 text-small">
                      <CircleDot className="mt-0.5 size-3.5 shrink-0 text-warn" aria-hidden />
                      {r.text ?? String(r)}
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="relative mt-4 flex items-center gap-2 font-medium">
                <span className="grid size-5 place-items-center rounded-full bg-ok/15 text-ok">
                  <Check className="size-3.5" aria-hidden data-status-icon />
                </span>
                {t('webSetConfig.finishReady')}
              </p>
            )}
            {commands.length ? (
              <div className="relative mt-3 flex flex-col gap-2">
                {commands.map((c, i) => (
                  <CommandLine
                    key={i}
                    command={`${c.command}${configArg}`}
                    onCopy={(text) => void navigator.clipboard?.writeText(text)}
                  />
                ))}
              </div>
            ) : null}
            <div className="relative mt-5 flex justify-end border-t border-border pt-3.5">
              <Link to="/chat" className={buttonVariants({ variant: 'primary' })}>
                <MessageSquare aria-hidden />
                {t('webSetConfig.openChat')}
              </Link>
            </div>
          </SettingsCard>
        ) : null}
      </div>

      <div className="flex items-center justify-between gap-2">
        <Button variant="ghost" disabled={index === 0} onClick={() => setStep(ORDER[index - 1]!)}>
          <ArrowLeft />
          {t('webSetConfig.back')}
        </Button>
        <Button
          variant="outline"
          disabled={index === ORDER.length - 1}
          onClick={() => setStep(ORDER[index + 1]!)}
        >
          {t('webSetConfig.next')}
          <ArrowRight />
        </Button>
      </div>
    </div>
  )
}

/** `/settings/setup`: the guided setup stepper (provider, router, extras, finish). */
export function SetupSection() {
  return <SnapshotGate>{(snapshot) => <Stepper snapshot={snapshot} />}</SnapshotGate>
}
