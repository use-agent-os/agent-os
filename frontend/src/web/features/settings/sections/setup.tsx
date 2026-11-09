import { Check, Copy } from 'lucide-react'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { t } from '@/i18n'
import '@/i18n/en/webSetConfig'
import { cn } from '@/lib/utils'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import {
  configCliArg,
  finishEnvRecoveryCommands,
  finishSummary,
  initialStepFromStatus,
  onboardingReasons,
} from '@/views/setup/logic'
import { Button, buttonVariants } from '@/web/components/ui/button'
import { RouterForm, SnapshotGate, SpecForm } from './onboarding-forms'

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
    <div className="flex flex-col gap-4">
      <ol
        aria-label={t('webSetConfig.stepOf', {
          current: index + 1,
          total: ORDER.length,
          name: stepLabel(step),
        })}
        className="flex flex-wrap gap-2"
      >
        {ORDER.map((s, i) => (
          <li key={s}>
            <button
              type="button"
              aria-current={s === step ? 'step' : undefined}
              onClick={() => setStep(s)}
              className={cn(
                'target flex items-center gap-1.5 rounded-full border px-3 py-1 text-ui',
                s === step
                  ? 'border-accent bg-accent-soft font-medium'
                  : 'border-border text-text-muted',
              )}
            >
              {i < index ? (
                <Check className="size-3.5" aria-hidden />
              ) : (
                <span aria-hidden>{i + 1}</span>
              )}
              {stepLabel(s)}
            </button>
          </li>
        ))}
      </ol>

      {step === 'provider' ? (
        <SpecForm
          title={t('webSetConfig.provider')}
          specs={snapshot.catalog?.providers ?? []}
          current={config.llm?.provider ?? ''}
          method="onboarding.provider.configure"
          revision={snapshot.revision}
        />
      ) : null}
      {step === 'router' ? <RouterForm snapshot={snapshot} /> : null}
      {step === 'extras' ? (
        <div className="flex flex-col gap-4">
          <SpecForm
            title={t('webSetConfig.webSearch')}
            specs={snapshot.catalog?.searchProviders ?? []}
            current={config.search_provider ?? ''}
            method="onboarding.search.configure"
            revision={snapshot.revision}
          />
          <SpecForm
            title={t('webSetConfig.embedding')}
            specs={snapshot.catalog?.memoryEmbeddingProviders ?? []}
            current={status.memoryEmbeddingProvider ?? ''}
            method="onboarding.memory_embedding.configure"
            revision={snapshot.revision}
          />
          <SpecForm
            title={t('webSetConfig.image')}
            specs={snapshot.catalog?.imageGenerationProviders ?? []}
            current={status.imageGenerationProvider ?? ''}
            method="onboarding.imageGeneration.configure"
            revision={snapshot.revision}
          />
          <SpecForm
            title={t('webSetConfig.audio')}
            specs={snapshot.catalog?.audioProviders ?? []}
            current={status.audioProvider ?? ''}
            method="onboarding.audio.configure"
            revision={snapshot.revision}
          />
        </div>
      ) : null}
      {step === 'finish' ? (
        <section
          aria-label={t('webSetConfig.finishTitle')}
          className="rounded-lg border border-border p-4"
        >
          <h2 className="mb-2 font-semibold">{t('webSetConfig.finishTitle')}</h2>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
            {Object.entries(summary).map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-text-muted">{k}</dt>
                <dd>{v || '—'}</dd>
              </div>
            ))}
          </dl>
          {reasons.length ? (
            <div className="mt-3">
              <p className="font-medium">{t('webSetConfig.finishActions')}</p>
              <ul className="ml-4 list-disc">
                {reasons.map((r, i) => (
                  <li key={i}>{r.text ?? String(r)}</li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="mt-3">{t('webSetConfig.finishReady')}</p>
          )}
          {commands.map((c, i) => (
            <div key={i} className="mt-2 flex items-center gap-2">
              <code className="min-w-0 flex-1 truncate rounded bg-code-bg px-2 py-1 text-small">{`${c.command}${configArg}`}</code>
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={t('webSetConfig.copyCommand')}
                onClick={() => void navigator.clipboard?.writeText(`${c.command}${configArg}`)}
              >
                <Copy />
              </Button>
            </div>
          ))}
          <Link to="/chat" className={cn(buttonVariants({ variant: 'primary' }), 'mt-4')}>
            {t('webSetConfig.openChat')}
          </Link>
        </section>
      ) : null}

      <div className="flex gap-2">
        <Button variant="outline" disabled={index === 0} onClick={() => setStep(ORDER[index - 1]!)}>
          {t('webSetConfig.back')}
        </Button>
        <Button
          variant="secondary"
          disabled={index === ORDER.length - 1}
          onClick={() => setStep(ORDER[index + 1]!)}
        >
          {t('webSetConfig.next')}
        </Button>
      </div>
    </div>
  )
}

/** `/settings/setup`: the guided setup stepper (provider, router, extras, finish). */
export function SetupSection() {
  return <SnapshotGate>{(snapshot) => <Stepper snapshot={snapshot} />}</SnapshotGate>
}
