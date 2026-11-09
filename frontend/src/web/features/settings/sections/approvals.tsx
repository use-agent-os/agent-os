import { useEffect, useState } from 'react'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webSetAccess'
import { gate, requireConnection, type Gate } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  approvalMonitor,
  saveApprovalMode,
  setBrowserElevated,
  useApprovals,
  type ElevatedMode,
} from '@/services/approval-monitor'
import {
  approvalCardDetail,
  modeOptions,
  modeStateTone,
  resolveExecutionMode,
} from '@/views/approvals/logic'
import { configValueAt } from '@/views/config/logic'
import { EmptyState } from '@/web/components/ui/states'
import { ApprovalCard } from '@/web/shell/ApprovalTray'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useConfigEditor } from '../config-editor'
import { ConfigField, useRegisterFields } from '../fields'
import { SaveBar } from '../SaveBar'

// Settings › Approvals (overhaul-control-ui 7.7, Appendix A "Approvals"): the
// global default mode (gateway config, explicit save), the approval strategy
// (/api/approvals/settings, saved at once), this browser's elevated override,
// and the pending requests with their full arguments.

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function useWriteGate(): Gate {
  return gate(requireConnection(useDisplayedConnection()))
}

function GateReason({ gate: g, className }: { gate: Gate; className?: string }) {
  return g.reason ? <p className={cn('text-small text-text-muted', className)}>{g.reason}</p> : null
}

const TONE_BORDER = { ok: 'border-accent', warn: 'border-warn', danger: 'border-danger' } as const

interface Choice {
  value: string
  label: string
  desc: string
}

/** A radio group of cards; `tone` colours the selected card. */
function ChoiceGroup({
  name,
  label,
  choices,
  value,
  disabled,
  tone = () => 'ok',
  onChange,
}: {
  name: string
  label: string
  choices: readonly Choice[]
  value: string
  disabled?: boolean
  tone?: (value: string) => keyof typeof TONE_BORDER
  onChange: (value: string) => void
}) {
  return (
    <div role="radiogroup" aria-label={label} className="grid gap-2 sm:grid-cols-2">
      {choices.map((choice) => {
        const checked = choice.value === value
        return (
          <label
            key={choice.value}
            className={cn(
              'flex cursor-pointer items-start gap-2 rounded-md border p-3 has-disabled:cursor-not-allowed has-disabled:opacity-60',
              checked ? cn(TONE_BORDER[tone(choice.value)], 'bg-surface-2') : 'border-border',
            )}
          >
            <input
              type="radio"
              name={name}
              value={choice.value}
              checked={checked}
              disabled={disabled}
              onChange={() => onChange(choice.value)}
              className="mt-1"
            />
            <span>
              <span className="block font-medium">{choice.label}</span>
              <span className="block text-small text-text-muted">{choice.desc}</span>
            </span>
          </label>
        )
      })}
    </div>
  )
}

function StrategySetting({ write }: { write: Gate }) {
  const storeMode = useApprovals((s) => s.mode)
  // Optimistic: the new choice shows at once and reverts if the save fails.
  const [pendingMode, setPendingMode] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mode = pendingMode ?? storeMode

  const select = async (next: string) => {
    if (next === mode) return
    setPendingMode(next)
    setSaving(true)
    setError(null)
    try {
      await saveApprovalMode(next)
      await approvalMonitor.pollNow()
    } catch (e) {
      setError(t('webSetAccess.apStrategyFailed', { message: errorMessage(e) }))
    } finally {
      setPendingMode(null)
      setSaving(false)
    }
  }

  return (
    <section
      aria-labelledby="approvals-strategy"
      data-field-id="approvals.strategy"
      className="flex flex-col gap-2"
    >
      <h2 id="approvals-strategy" className="text-base font-semibold">
        {t('webSetAccess.apStrategyTitle')}
      </h2>
      <p className="text-small text-text-muted">{t('webSetAccess.apStrategyHelp')}</p>
      <ChoiceGroup
        name="approval-strategy"
        label={t('webSetAccess.apStrategyTitle')}
        choices={modeOptions()}
        value={mode}
        disabled={saving || write.disabled}
        tone={modeStateTone}
        onChange={(next) => void select(next)}
      />
      <GateReason gate={write} />
      {error ? (
        <p role="alert" className="text-small text-danger">
          {error}
        </p>
      ) : null}
    </section>
  )
}

function elevatedChoices(): Choice[] {
  return [
    {
      value: '',
      label: t('webSetAccess.apElevatedOff'),
      desc: t('webSetAccess.apElevatedOffDesc'),
    },
    {
      value: 'on',
      label: t('webSetAccess.apElevatedOn'),
      desc: t('webSetAccess.apElevatedOnDesc'),
    },
    {
      value: 'bypass',
      label: t('webSetAccess.apElevatedBypass'),
      desc: t('webSetAccess.apElevatedBypassDesc'),
    },
    {
      value: 'full',
      label: t('webSetAccess.apElevatedFull'),
      desc: t('webSetAccess.apElevatedFullDesc'),
    },
  ]
}

function ElevatedSetting({ globalMode }: { globalMode: string }) {
  const elevated = useApprovals((s) => s.elevatedMode)
  const execution = resolveExecutionMode(elevated, globalMode)
  return (
    <section
      aria-labelledby="approvals-elevated"
      data-field-id="approvals.elevated"
      className="flex flex-col gap-2"
    >
      <h2 id="approvals-elevated" className="text-base font-semibold">
        {t('webSetAccess.apElevatedTitle')}
      </h2>
      <p className="text-small text-text-muted">{t('webSetAccess.apElevatedHelp')}</p>
      {/* Stored in this browser only; nothing is sent until a chat runs. */}
      <ChoiceGroup
        name="browser-elevated"
        label={t('webSetAccess.apElevatedTitle')}
        choices={elevatedChoices()}
        value={elevated}
        tone={(v) => (v === 'full' ? 'danger' : v === 'bypass' ? 'warn' : 'ok')}
        onChange={(next) => setBrowserElevated(next as ElevatedMode)}
      />
      <div role="status" className="rounded-md border border-border p-3">
        <p className="text-small text-text-muted">{t('webSetAccess.apEffective')}</p>
        <p className="font-medium">{execution.label}</p>
        <p className="text-small text-text-muted">{execution.desc}</p>
      </div>
    </section>
  )
}

function PendingRequests({ write }: { write: Gate }) {
  const pending = useApprovals((s) => s.pending)
  return (
    <section aria-labelledby="approvals-pending" className="flex flex-col gap-2">
      <h2 id="approvals-pending" className="text-base font-semibold">
        {pending.length
          ? tPlural('webSetAccess.apPendingCount', pending.length)
          : t('webSetAccess.apPendingTitle')}
      </h2>
      <GateReason gate={write} />
      {pending.length === 0 ? (
        <EmptyState
          className="py-6"
          title={t('webSetAccess.apEmptyTitle')}
          description={t('webSetAccess.apEmptyBody')}
        />
      ) : (
        <div className="flex flex-col gap-3">
          {pending.map((item) => {
            const full = approvalCardDetail(item)
            return (
              <div key={item.id} className="flex flex-col gap-1">
                <ul role="list">
                  <ApprovalCard item={item} />
                </ul>
                {full ? (
                  <details className="px-1">
                    <summary className="cursor-pointer text-small text-text-muted">
                      {t('webSetAccess.apFullArgs')}
                    </summary>
                    <pre
                      tabIndex={0}
                      className="mt-1 max-h-96 overflow-auto rounded bg-surface-0 p-2 font-mono text-small whitespace-pre-wrap"
                    >
                      {full}
                    </pre>
                  </details>
                ) : null}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

export function ApprovalsSection() {
  const editor = useConfigEditor()
  const write = useWriteGate()

  useRegisterFields('approvals', [
    { id: 'permissions.default_mode', label: t('webSetAccess.apDefaultMode') },
    { id: 'permissions.cron_default_mode', label: t('webSetAccess.apCronMode') },
    { id: 'approvals.strategy', label: t('webSetAccess.apStrategyTitle') },
    { id: 'approvals.elevated', label: t('webSetAccess.apElevatedTitle') },
  ])

  // The monitor publishes the strategy and pending list; refresh on entry.
  useEffect(() => {
    void approvalMonitor.pollNow()
  }, [])

  const savedDefault = String(configValueAt(editor.config, 'permissions.default_mode') ?? '')
  const configDisabled = !editor.ready || write.disabled

  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="approvals-default" className="flex flex-col">
        <h2 id="approvals-default" className="text-base font-semibold">
          {t('webSetAccess.apDefaultTitle')}
        </h2>
        <ConfigField
          editor={editor}
          path="permissions.default_mode"
          label={t('webSetAccess.apDefaultMode')}
          help={t('webSetAccess.apDefaultModeHelp')}
          type={{ kind: 'enum', options: ['off', 'on', 'bypass', 'full'] }}
          disabled={configDisabled}
        />
        <ConfigField
          editor={editor}
          path="permissions.cron_default_mode"
          label={t('webSetAccess.apCronMode')}
          help={t('webSetAccess.apCronModeHelp')}
          type={{ kind: 'enum', options: ['off', 'bypass', 'full'] }}
          disabled={configDisabled}
        />
        <GateReason gate={write} />
      </section>
      <StrategySetting write={write} />
      <ElevatedSetting globalMode={savedDefault} />
      <PendingRequests write={write} />
      <SaveBar editor={editor} />
    </div>
  )
}
