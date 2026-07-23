import { useId, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '@/components/ui/button'
import {
  buildSavePayload,
  explainCron,
  humanCountdown,
  humanTime,
  nextRuns,
  parseCron,
  resolveTarget,
  seedForm,
  type CronForm,
  type DeliveryMode,
  type FailureDestMode,
  type PayloadKind,
  type RawJob,
  type SaveBuild,
  type ScheduleKind,
  type SessionTarget,
} from './logic'

// cron.js:105-108 — the four cron presets.
const PRESETS: Array<{ cron: string; label: string }> = [
  { cron: '*/5 * * * *', label: 'Every 5m' },
  { cron: '0 * * * *', label: 'Hourly' },
  { cron: '0 9 * * 1-5', label: 'Weekdays 09:00' },
  { cron: '0 0 * * 0', label: 'Sundays midnight' },
]

// cron.js:88-92 — schedule type options.
const SCHEDULE_TYPES: Array<{ value: ScheduleKind; label: string }> = [
  { value: 'cron', label: 'Cron expression' },
  { value: 'every', label: 'Fixed interval' },
  { value: 'at', label: 'One-time ISO time' },
]

// cron.js:130-134 — job mode options.
const JOB_MODES: Array<{ value: PayloadKind; label: string }> = [
  { value: 'reminder', label: 'Static Reminder (no model)' },
  { value: 'agent_turn', label: 'Background Agent Task (choose session)' },
  { value: 'system_event', label: 'System Event (Main)' },
]

// cron.js:145-150 — session target options.
const SESSION_TARGETS: Array<{ value: SessionTarget; label: string }> = [
  { value: 'main', label: 'Agent main session' },
  { value: 'current', label: 'Current chat session' },
  { value: 'isolated', label: 'Isolated cron session' },
  { value: 'session', label: 'Named session' },
]

// cron.js:179-184 — delivery mode options.
const DELIVERY_MODES: Array<{ value: DeliveryMode; label: string }> = [
  { value: '', label: 'Default (inferred from session)' },
  { value: 'none', label: 'None (run silently)' },
  { value: 'announce', label: 'Announce to channel' },
  { value: 'webhook', label: 'Post to webhook' },
]

// cron.js:220-224 — failure-destination mode options.
const FD_MODES: Array<{ value: FailureDestMode; label: string }> = [
  { value: '', label: 'Disabled (no separate failure alert)' },
  { value: 'channel', label: 'A channel' },
  { value: 'webhook', label: 'A webhook' },
]

// cron.js:169-173 — wake-mode options.
const WAKE_MODES: Array<{ value: string; label: string }> = [
  { value: 'now', label: 'Now (fire immediately on schedule)' },
  { value: 'next-heartbeat', label: 'Next heartbeat (defer to main loop)' },
]

function CronExplain({ expr }: { expr: string }) {
  // cron.js:1401-1442 — live human summary + up to 3 upcoming runs.
  const trimmed = expr.trim()
  const summary = useMemo(() => {
    if (!trimmed) return null
    const parsed = parseCron(trimmed)
    if (!parsed) return { invalid: true as const }
    return {
      invalid: false as const,
      text: explainCron(trimmed) || 'matches a custom cadence',
      parsed,
    }
  }, [trimmed])

  const upcoming = useMemo(() => {
    if (!summary || summary.invalid) return []
    return nextRuns(summary.parsed, 3)
  }, [summary])

  if (!trimmed) {
    return (
      <div className="cron-explain__human t-data">Enter a 5-field cron expression to preview</div>
    )
  }
  if (summary?.invalid) {
    return (
      <div className="cron-explain__human cron-explain--invalid t-data">
        Could not parse expression — expected 5 fields (m h dom mon dow).
      </div>
    )
  }
  return (
    <div className="cron-explain">
      <div className="cron-explain__human cron-explain--valid t-data">{summary?.text}</div>
      {upcoming.length ? (
        <ul className="cron-explain__upcoming">
          {upcoming.map((d, i) => (
            <li key={i}>
              <span className="cron-mono">{humanCountdown(d)}</span>
              <span className="cron-explain__abs">{humanTime(d)}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

export function CronPanel({
  job,
  template,
  activeSessionKey,
  saving,
  onCancel,
  onSubmit,
}: {
  job: RawJob | null
  template: Partial<RawJob> | null
  activeSessionKey: string
  saving: boolean
  onCancel: () => void
  onSubmit: (build: Extract<SaveBuild, { ok: true }>) => void
}) {
  const [form, setForm] = useState<CronForm>(() => seedForm(job, template, activeSessionKey))
  const [error, setError] = useState<string | null>(null)
  const titleId = useId()
  const isEdit = !!job

  function set<K extends keyof CronForm>(key: K, value: CronForm[K]) {
    setForm((f) => ({ ...f, [key]: value }))
  }

  // cron.js:997-1057 — the resolved session target (locked flags + message label).
  const targetRes = resolveTarget(form.payloadKind, form.sessionTarget, activeSessionKey)

  const isAnnounce = form.deliveryMode === 'announce'
  const isWebhook = form.deliveryMode === 'webhook'
  const showBestEffort = isAnnounce || isWebhook
  const isFdChannel = form.fdMode === 'channel'
  const isFdWebhook = form.fdMode === 'webhook'

  function submit(e: React.FormEvent) {
    e.preventDefault()
    // Persist the resolved (possibly coerced) target before building.
    const effectiveForm: CronForm = { ...form, sessionTarget: targetRes.target }
    const build = buildSavePayload(effectiveForm, job, activeSessionKey)
    if (!build.ok) {
      // cron.js:1182,1201,1207,1227,1236 — validation failures surface as a warn
      // toast (legacy UI.toast(..,'warn')); also shown inline for visibility.
      setError(build.error)
      toast.warning(build.error, { id: 'cron-save-validate' })
      return
    }
    setError(null)
    onSubmit(build)
  }

  return (
    <ModalShell
      role="dialog"
      labelledBy={titleId}
      onClose={onCancel}
      dismissible={!saving}
      overlayClassName="cron-modal__overlay"
      className="cron-panel panel"
    >
      {/* noValidate: validation is JS-only (buildSavePayload), matching the
            legacy view — native constraints (e.g. number min) must not
            intercept submit before our validators run. */}
      <form className="cron-panel__form" noValidate onSubmit={submit}>
        <header className="cron-panel__head">
          <span className="t-label">{isEdit ? 'Edit schedule' : 'New schedule'}</span>
          <h2 id={titleId} className="cron-panel__title">
            {isEdit ? 'Edit Schedule' : 'Create a job'}
          </h2>
        </header>

        <div className="cron-panel__body">
          <label className="cron-field">
            <span className="t-label">Name</span>
            <input
              id="cp-name"
              className="cron-input"
              type="text"
              autoComplete="off"
              placeholder="my-job"
              value={form.name}
              onChange={(e) => set('name', e.target.value)}
            />
          </label>

          <label className="cron-field">
            <span className="t-label">Schedule type</span>
            <select
              className="cron-input"
              value={form.scheduleKind}
              onChange={(e) => set('scheduleKind', e.target.value as ScheduleKind)}
            >
              {SCHEDULE_TYPES.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>

          {form.scheduleKind === 'cron' ? (
            <div className="cron-field">
              <label className="t-label" htmlFor="cp-cron">
                Cron expression
              </label>
              <input
                id="cp-cron"
                className="cron-input cron-input--mono"
                type="text"
                autoComplete="off"
                spellCheck={false}
                placeholder="0 9 * * 1-5"
                value={form.cron}
                onChange={(e) => set('cron', e.target.value)}
              />
              <CronExplain expr={form.cron} />
              <div className="cron-presets">
                <span className="cron-presets__label t-label">Presets</span>
                {PRESETS.map((p) => (
                  <button
                    key={p.cron}
                    type="button"
                    className="cron-preset"
                    onClick={() => set('cron', p.cron)}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {form.scheduleKind === 'every' ? (
            <label className="cron-field">
              <span className="t-label">Interval (seconds)</span>
              <input
                className="cron-input"
                type="number"
                min="1"
                placeholder="60"
                value={form.every}
                onChange={(e) => set('every', e.target.value)}
              />
            </label>
          ) : null}

          {form.scheduleKind === 'at' ? (
            <label className="cron-field">
              <span className="t-label">ISO time</span>
              <input
                className="cron-input cron-input--mono"
                type="text"
                placeholder="2026-05-18T09:00:00+08:00"
                value={form.at}
                onChange={(e) => set('at', e.target.value)}
              />
            </label>
          ) : null}

          <label className="cron-field">
            <span className="t-label">Timezone (IANA)</span>
            <input
              className="cron-input cron-input--mono"
              type="text"
              autoComplete="off"
              spellCheck={false}
              placeholder="America/Los_Angeles"
              value={form.tz}
              onChange={(e) => set('tz', e.target.value)}
            />
            <span className="cron-field__hint">
              Leave empty to evaluate the cron expression in UTC.
            </span>
          </label>

          <label className="cron-field">
            <span className="t-label">Job mode</span>
            <select
              className="cron-input"
              value={form.payloadKind}
              onChange={(e) => set('payloadKind', e.target.value as PayloadKind)}
            >
              {JOB_MODES.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>

          <label className="cron-field">
            <span className="t-label">Agent ID</span>
            <input
              className="cron-input"
              type="text"
              placeholder="main"
              value={form.agentId}
              onChange={(e) => set('agentId', e.target.value)}
            />
          </label>

          <label className="cron-field">
            <span className="t-label">Session target</span>
            <select
              className="cron-input"
              value={targetRes.target}
              disabled={targetRes.locked}
              onChange={(e) => set('sessionTarget', e.target.value as SessionTarget)}
            >
              {SESSION_TARGETS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>

          {targetRes.showTargetSessionRow ? (
            <label className="cron-field">
              <span className="t-label">
                {targetRes.target === 'current' ? 'Current session key' : 'Named session key'}
              </span>
              <input
                className="cron-input"
                type="text"
                placeholder="agent:main:webchat:abc123"
                value={form.targetSessionKey}
                onChange={(e) => set('targetSessionKey', e.target.value)}
              />
            </label>
          ) : null}

          <label className="cron-field">
            <span className="t-label">{targetRes.messageLabel}</span>
            <textarea
              className="cron-input cron-input--textarea"
              rows={4}
              placeholder="Run daily report…"
              value={form.message}
              onChange={(e) => set('message', e.target.value)}
            />
          </label>

          <details className="cron-advanced">
            <summary className="cron-advanced__summary">Advanced delivery &amp; wake</summary>
            <div className="cron-advanced__body">
              <label className="cron-field">
                <span className="t-label">Wake mode</span>
                <select
                  className="cron-input"
                  value={form.wakeMode}
                  onChange={(e) => set('wakeMode', e.target.value)}
                >
                  {WAKE_MODES.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>

              <label className="cron-field">
                <span className="t-label">Delivery mode</span>
                <select
                  className="cron-input"
                  value={form.deliveryMode}
                  onChange={(e) => set('deliveryMode', e.target.value as DeliveryMode)}
                >
                  {DELIVERY_MODES.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>

              {isAnnounce ? (
                <>
                  <label className="cron-field">
                    <span className="t-label">Channel</span>
                    <input
                      className="cron-input"
                      type="text"
                      placeholder="slack"
                      value={form.deliveryChannel}
                      onChange={(e) => set('deliveryChannel', e.target.value)}
                    />
                  </label>
                  <label className="cron-field">
                    <span className="t-label">Recipient</span>
                    <input
                      className="cron-input"
                      type="text"
                      placeholder="C-team-alerts"
                      value={form.deliveryTo}
                      onChange={(e) => set('deliveryTo', e.target.value)}
                    />
                  </label>
                  <label className="cron-field">
                    <span className="t-label">Account id</span>
                    <input
                      className="cron-input"
                      type="text"
                      value={form.deliveryAccount}
                      onChange={(e) => set('deliveryAccount', e.target.value)}
                    />
                  </label>
                </>
              ) : null}

              {isWebhook ? (
                <>
                  <label className="cron-field">
                    <span className="t-label">Webhook URL</span>
                    <input
                      className="cron-input cron-input--mono"
                      type="url"
                      placeholder="https://hooks.example/cron"
                      value={form.deliveryWebhookUrl}
                      onChange={(e) => set('deliveryWebhookUrl', e.target.value)}
                    />
                  </label>
                  <label className="cron-field">
                    <span className="t-label">Webhook bearer token</span>
                    <input
                      className="cron-input"
                      type="password"
                      placeholder="optional bearer token"
                      value={form.deliveryWebhookToken}
                      onChange={(e) => set('deliveryWebhookToken', e.target.value)}
                    />
                  </label>
                </>
              ) : null}

              {showBestEffort ? (
                <label className="cron-toggle">
                  <input
                    type="checkbox"
                    checked={form.deliveryBestEffort}
                    onChange={(e) => set('deliveryBestEffort', e.target.checked)}
                  />
                  <span>Best-effort delivery (do not fail the job when delivery fails)</span>
                </label>
              ) : null}

              <details className="cron-advanced cron-advanced--nested">
                <summary className="cron-advanced__summary">Failure destination</summary>
                <div className="cron-advanced__body">
                  <label className="cron-field">
                    <span className="t-label">Route failures to</span>
                    <select
                      className="cron-input"
                      value={form.fdMode}
                      onChange={(e) => set('fdMode', e.target.value as FailureDestMode)}
                    >
                      {FD_MODES.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  </label>

                  {isFdChannel ? (
                    <>
                      <label className="cron-field">
                        <span className="t-label">Channel</span>
                        <input
                          className="cron-input"
                          type="text"
                          placeholder="slack"
                          value={form.fdChannel}
                          onChange={(e) => set('fdChannel', e.target.value)}
                        />
                      </label>
                      <label className="cron-field">
                        <span className="t-label">Recipient</span>
                        <input
                          className="cron-input"
                          type="text"
                          placeholder="C-ops-alerts"
                          value={form.fdTo}
                          onChange={(e) => set('fdTo', e.target.value)}
                        />
                      </label>
                      <label className="cron-field">
                        <span className="t-label">Account id</span>
                        <input
                          className="cron-input"
                          type="text"
                          value={form.fdAccount}
                          onChange={(e) => set('fdAccount', e.target.value)}
                        />
                      </label>
                    </>
                  ) : null}

                  {isFdWebhook ? (
                    <>
                      <label className="cron-field">
                        <span className="t-label">Webhook URL</span>
                        <input
                          className="cron-input cron-input--mono"
                          type="url"
                          placeholder="https://hooks.example/alert"
                          value={form.fdWebhookUrl}
                          onChange={(e) => set('fdWebhookUrl', e.target.value)}
                        />
                      </label>
                      <label className="cron-field">
                        <span className="t-label">Webhook bearer token</span>
                        <input
                          className="cron-input"
                          type="password"
                          placeholder="optional bearer token"
                          value={form.fdWebhookToken}
                          onChange={(e) => set('fdWebhookToken', e.target.value)}
                        />
                      </label>
                    </>
                  ) : null}
                </div>
              </details>
            </div>
          </details>

          <label className="cron-toggle">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => set('enabled', e.target.checked)}
            />
            <span>Enabled</span>
          </label>

          {error ? (
            <p className="cron-panel__error" role="alert">
              {error}
            </p>
          ) : null}
        </div>

        <footer className="cron-panel__foot">
          <Button type="button" variant="ghost" disabled={saving} onClick={onCancel}>
            Cancel
          </Button>
          <Button type="submit" disabled={saving}>
            Save schedule
          </Button>
        </footer>
      </form>
    </ModalShell>
  )
}
