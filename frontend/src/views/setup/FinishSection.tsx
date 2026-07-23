// Finish section (setup.js:1077-1288). Provider/model/router/channels summary,
// three CLI command groups (Fix now / CLI handoff / CLI recipes), the readiness
// summary with per-section fix buttons, and the update-notify preference
// (config.patch {updates.notify}).
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { CommandLine } from '@/components/CommandLine'
import { SetupCheckbox } from './parts'
import {
  configCliArg,
  envFixCommands,
  finishEnvRecoveryCommands,
  finishSummary,
  handoffCommands,
  readinessActionLabel,
  readinessStatusLabel,
  readinessTone,
  recipeCommands,
  setupStepForSection,
  type CliCommand,
  type OnboardingStatus,
  type SectionDetail,
  type SetupConfig,
  type StepId,
} from './logic'

const TONE_CLASS: Record<string, string> = {
  'is-ok': 'tone-ok',
  'is-warn': 'tone-warn',
  'is-muted': 'tone-dim',
}

function CommandGroup({ title, commands }: { title: string; commands: CliCommand[] }) {
  if (!commands.length) return null
  return (
    <section className="setup-cli__group" aria-label={title}>
      <h3 className="t-label">{title}</h3>
      {commands.map((c) => (
        <div className="setup-cli__row" key={c.label}>
          <span className="setup-cli__label t-label">{c.label}</span>
          <CommandLine command={c.command} toastIdPrefix="setup-copy" />
        </div>
      ))}
    </section>
  )
}

function ReadinessGroup({
  title,
  entries,
  onGo,
}: {
  title: string
  entries: Array<[string, SectionDetail]>
  onGo: (step: StepId) => void
}) {
  if (!entries.length) return null
  return (
    <div className="setup-readiness__group">
      <h3 className="t-label">{title}</h3>
      {entries.map(([name, detail]) => {
        const step = setupStepForSection(name, detail)
        return (
          <div
            className={`setup-readiness__row ${TONE_CLASS[readinessTone(detail, name)]}`}
            key={name}
          >
            <span>{detail.label || name}</span>
            <strong>{readinessStatusLabel(detail, name)}</strong>
            <small>{detail.required ? 'Required' : 'Optional'}</small>
            {step ? (
              <Button type="button" size="sm" variant="outline" onClick={() => onGo(step)}>
                {readinessActionLabel(detail, name)}
              </Button>
            ) : null}
            {detail.detail ? <em className="setup-readiness__detail">{detail.detail}</em> : null}
          </div>
        )
      })}
    </div>
  )
}

export function FinishSection({
  status,
  config,
  onBack,
  onReload,
  onExit,
  onGoStep,
  onSaveUpdatesNotify,
  saving,
}: {
  status: OnboardingStatus
  config: SetupConfig
  onBack: () => void
  onReload: () => void
  onExit: () => void
  onGoStep: (step: StepId) => void
  onSaveUpdatesNotify: (notify: boolean) => void
  saving: boolean
}) {
  const summary = finishSummary(status, config)
  const configArg = configCliArg(status.configPath)
  const fixCommands = envFixCommands(finishEnvRecoveryCommands(status), configArg)

  const details = Object.entries(status.sectionDetails || {})
  const required = details.filter(([, d]) => d.required)
  const optional = details.filter(([, d]) => !d.required)

  const [notify, setNotify] = useState((config.updates || {}).notify !== false)

  return (
    <section className="setup-panel panel">
      <header className="setup-panel__head">
        <h2 className="t-label">Finish</h2>
        <p className="setup-panel__subtitle">{status.configPath || ''}</p>
      </header>

      <div className="setup-cli">
        <CommandGroup title="Fix now" commands={fixCommands} />
        <CommandGroup title="CLI handoff" commands={handoffCommands(configArg)} />
        <CommandGroup title="CLI recipes" commands={recipeCommands(configArg)} />
      </div>

      <div className="setup-summary">
        <div>
          <span className="t-label">Provider</span>
          <strong>{summary.provider}</strong>
        </div>
        <div>
          <span className="t-label">Model</span>
          <strong>{summary.model}</strong>
        </div>
        {summary.proxy ? (
          <div>
            <span className="t-label">Proxy</span>
            <strong>{summary.proxy}</strong>
          </div>
        ) : null}
        <div>
          <span className="t-label">Router</span>
          <strong>{summary.router}</strong>
        </div>
        <div>
          <span className="t-label">Channels</span>
          <strong>{summary.channels}</strong>
        </div>
      </div>

      {details.length ? (
        <div className="setup-readiness" aria-label="Onboarding readiness">
          <ReadinessGroup title="Required setup" entries={required} onGo={onGoStep} />
          <ReadinessGroup title="Optional capabilities" entries={optional} onGo={onGoStep} />
        </div>
      ) : null}

      <section className="setup-subpanel" aria-label="Update preferences">
        <h3 className="t-label">Updates</h3>
        <SetupCheckbox ariaLabel="Notify on new release" checked={notify} onChange={setNotify}>
          Notify me when a new release of use-agent-os is available
        </SetupCheckbox>
        <div className="setup-actions">
          <Button
            type="button"
            variant="outline"
            disabled={saving}
            onClick={() => onSaveUpdatesNotify(notify)}
          >
            Save update preference
          </Button>
        </div>
      </section>

      <div className="setup-actions">
        <Button type="button" variant="outline" onClick={onBack}>
          Back
        </Button>
        <Button type="button" variant="outline" onClick={onReload}>
          Refresh
        </Button>
        <Button type="button" onClick={onExit}>
          Open Overview
        </Button>
      </div>
    </section>
  )
}
