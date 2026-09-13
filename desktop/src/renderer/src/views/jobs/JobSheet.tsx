import { ShieldAlert, TriangleAlert } from 'lucide-react'
import { useId, useState } from 'react'
import { ModalShell } from '@/components/ModalShell'
import {
  buildSavePayload,
  parseCron,
  resolveTarget,
  seedForm,
  targetsForChannel,
  type CronForm,
  type DeliveryMode,
  type DeliveryTarget,
  type DeliveryTargetMap,
  type FailureDestMode,
  type PayloadKind,
  type RawJob,
  type SaveBuild,
} from '@/views/cron/logic'
import { Button } from '~/components/ui/button'
import { Switch } from '~/components/ui/switch'
import { t, type MessageKey } from '~/i18n'
import { useSessions } from '~/stores/sessions'
import {
  DEFAULT_NATURAL,
  localTimeZone,
  naturalToCron,
  seedBuilder,
  type BuilderState,
} from './logic'
import { ScheduleEditor } from './ScheduleEditor'

export type SheetState =
  | { kind: 'closed' }
  | { kind: 'create'; template: Partial<RawJob> | null }
  | { kind: 'edit'; job: RawJob }

const KINDS: readonly PayloadKind[] = ['reminder', 'agent_turn', 'script', 'system_event']

const DELIVERY_MODES: ReadonlyArray<{ value: DeliveryMode; label: MessageKey }> = [
  { value: '', label: 'jobs.delivery.inferred' },
  { value: 'none', label: 'jobs.delivery.none' },
  { value: 'announce', label: 'jobs.delivery.announce' },
  { value: 'webhook', label: 'jobs.delivery.webhook' },
]

const FAILURE_MODES: ReadonlyArray<{ value: FailureDestMode; label: MessageKey }> = [
  { value: '', label: 'jobs.delivery.failure.none' },
  { value: 'channel', label: 'jobs.delivery.failure.channel' },
  { value: 'webhook', label: 'jobs.delivery.failure.webhook' },
]

/** Sentinel option: the recipient is not in the list, type it. */
const MANUAL = '__manual__'

/**
 * Create / edit as a sheet. The form model and the save payload are the
 * console's (`seedForm`, `buildSavePayload`) so both clients speak the same
 * wire; the layout, the schedule builder and the session picker are the
 * desktop's. A new job defaults to the Mac's own time zone.
 */
export function JobSheet({
  state,
  saving,
  saveError,
  deliveryTargets,
  onCancel,
  onSubmit,
}: {
  state: Exclude<SheetState, { kind: 'closed' }>
  saving: boolean
  /** The gateway's reason for refusing the last save, if any. */
  saveError?: string | null
  deliveryTargets?: DeliveryTargetMap
  onCancel: () => void
  onSubmit: (build: Extract<SaveBuild, { ok: true }>) => void
}) {
  const job = state.kind === 'edit' ? state.job : null
  const isEdit = job !== null
  const [form, setForm] = useState<CronForm>(() => {
    const seeded = seedForm(job, state.kind === 'create' ? state.template : null, '')
    if (!isEdit && !seeded.tz) seeded.tz = localTimeZone()
    // The builder opens on "every day at 09:00"; the expression must say so
    // too, or a job saved untouched would have no schedule at all.
    if (!isEdit && seeded.scheduleKind === 'cron' && !seeded.cron.trim()) {
      seeded.cron = naturalToCron(DEFAULT_NATURAL)
    }
    return seeded
  })
  const [builder, setBuilder] = useState<BuilderState>(() => seedBuilder(form, isEdit))
  const [error, setError] = useState<string | null>(null)
  const titleId = useId()

  function patch(next: Partial<CronForm>) {
    setForm((f) => ({ ...f, ...next }))
  }

  const target = resolveTarget(form.payloadKind, form.sessionTarget, '')
  const isScript = form.payloadKind === 'script'
  const isAgent = form.payloadKind === 'agent_turn'
  const canScript = isScript || isAgent
  const inSession = target.target === 'session' || target.target === 'current'

  function submit(e: React.FormEvent) {
    e.preventDefault()
    // The shared builder leaves cron syntax to the gateway; catching it here
    // keeps the error next to the field instead of in a toast after a round trip.
    if (form.scheduleKind === 'cron' && !parseCron(form.cron.trim())) {
      setError(t('jobs.schedule.invalid'))
      return
    }
    const build = buildSavePayload({ ...form, sessionTarget: target.target }, job, '')
    if (!build.ok) {
      setError(build.error)
      return
    }
    setError(null)
    onSubmit(build)
  }

  // A rejection from the gateway (a bad script path, an unreachable recipient)
  // belongs in the same slot as a local validation error.
  const shownError = error ?? saveError ?? null

  return (
    <ModalShell
      role="dialog"
      labelledBy={titleId}
      onClose={onCancel}
      dismissible={!saving}
      overlayClassName="jobs-sheet__overlay"
      className="jobs-sheet"
    >
      <form className="jobs-sheet__form" noValidate onSubmit={submit}>
        <header className="jobs-sheet__head">
          <h2 id={titleId}>{isEdit ? t('jobs.sheet.edit') : t('jobs.sheet.new')}</h2>
          <label className="jobs-sheet__enabled">
            <span>{t('jobs.field.enabled')}</span>
            <Switch
              checked={form.enabled}
              onCheckedChange={(enabled) => patch({ enabled })}
              aria-label={t('jobs.field.enabled')}
            />
          </label>
        </header>

        <div className="jobs-sheet__body">
          <div className="jobs-form__row">
            <label className="mac-label" htmlFor={`${titleId}-name`}>
              {t('jobs.field.name')}
            </label>
            <input
              id={`${titleId}-name`}
              className="mac-input"
              type="text"
              autoComplete="off"
              placeholder={t('jobs.field.name.placeholder')}
              value={form.name}
              onChange={(e) => patch({ name: e.target.value })}
            />
          </div>

          <Section title={t('jobs.section.schedule')}>
            <ScheduleEditor
              form={form}
              builder={builder}
              onForm={patch}
              onBuilder={(next) => setBuilder((b) => ({ ...b, ...next }))}
            />
          </Section>

          <Section title={t('jobs.section.task')}>
            <div className="jobs-form__stack">
              <div className="jobs-form__row jobs-form__row--top">
                <span className="mac-label">{t('jobs.task.kind')}</span>
                <div className="mac-field">
                  <div role="radiogroup" aria-label={t('jobs.task.kind')} className="mac-segmented">
                    {KINDS.map((k) => (
                      <button
                        key={k}
                        type="button"
                        role="radio"
                        aria-checked={form.payloadKind === k}
                        className="mac-segment"
                        onClick={() => patch({ payloadKind: k, elevated: false })}
                      >
                        {t(`jobs.kind.${k}`)}
                      </button>
                    ))}
                  </div>
                  <span className="mac-help">{t(`jobs.task.kind.${form.payloadKind}.help`)}</span>
                </div>
              </div>

              {!isScript ? (
                <div className="jobs-form__row jobs-form__row--top">
                  <label className="mac-label" htmlFor={`${titleId}-message`}>
                    {form.payloadKind === 'reminder'
                      ? t('jobs.task.message')
                      : form.payloadKind === 'system_event'
                        ? t('jobs.task.event')
                        : t('jobs.task.prompt')}
                  </label>
                  <textarea
                    id={`${titleId}-message`}
                    className="mac-textarea"
                    rows={4}
                    placeholder={
                      form.payloadKind === 'reminder'
                        ? t('jobs.task.message.placeholder')
                        : t('jobs.task.prompt.placeholder')
                    }
                    value={form.message}
                    onChange={(e) => patch({ message: e.target.value })}
                  />
                </div>
              ) : (
                <div className="jobs-form__row">
                  <label className="mac-label" htmlFor={`${titleId}-note`}>
                    {t('jobs.task.note')}
                  </label>
                  <input
                    id={`${titleId}-note`}
                    className="mac-input"
                    type="text"
                    placeholder={t('jobs.task.note.placeholder')}
                    value={form.message}
                    onChange={(e) => patch({ message: e.target.value })}
                  />
                </div>
              )}

              {canScript ? (
                <>
                  <div className="jobs-form__row">
                    <label className="mac-label" htmlFor={`${titleId}-script`}>
                      {isScript ? t('jobs.task.script') : t('jobs.task.preRun')}
                    </label>
                    <div className="mac-field">
                      <input
                        id={`${titleId}-script`}
                        className="mac-input"
                        data-mono="true"
                        type="text"
                        autoComplete="off"
                        spellCheck={false}
                        placeholder={isScript ? 'watch-memory.sh' : 'collect-context.py'}
                        value={form.script}
                        onChange={(e) => patch({ script: e.target.value })}
                      />
                      <span className="mac-help">
                        {isScript ? t('jobs.task.script.help') : t('jobs.task.preRun.help')}
                      </span>
                    </div>
                  </div>
                  {isScript || form.script.trim() ? (
                    <>
                      <div className="jobs-form__row">
                        <label className="mac-label" htmlFor={`${titleId}-args`}>
                          {t('jobs.task.args')}
                        </label>
                        <input
                          id={`${titleId}-args`}
                          className="mac-input"
                          data-mono="true"
                          type="text"
                          autoComplete="off"
                          spellCheck={false}
                          placeholder={t('jobs.task.args.placeholder')}
                          value={form.scriptArgs}
                          onChange={(e) => patch({ scriptArgs: e.target.value })}
                        />
                      </div>
                      <div className="jobs-form__row">
                        <label className="mac-label" htmlFor={`${titleId}-workdir`}>
                          {t('jobs.task.workdir')}
                        </label>
                        <input
                          id={`${titleId}-workdir`}
                          className="mac-input"
                          data-mono="true"
                          type="text"
                          autoComplete="off"
                          spellCheck={false}
                          placeholder={t('jobs.task.workdir.placeholder')}
                          value={form.workdir}
                          onChange={(e) => patch({ workdir: e.target.value })}
                        />
                      </div>
                      <Warning>{t('jobs.task.scriptWarning')}</Warning>
                    </>
                  ) : null}
                </>
              ) : null}

              {isAgent ? (
                <>
                  <div className="jobs-form__row jobs-form__row--top">
                    <span className="mac-label">{t('jobs.task.session')}</span>
                    <div className="mac-field">
                      <div
                        role="radiogroup"
                        aria-label={t('jobs.task.session')}
                        className="mac-segmented"
                      >
                        <button
                          type="button"
                          role="radio"
                          aria-checked={!inSession}
                          className="mac-segment"
                          onClick={() => patch({ sessionTarget: 'isolated' })}
                        >
                          {t('jobs.task.session.isolated')}
                        </button>
                        <button
                          type="button"
                          role="radio"
                          aria-checked={inSession}
                          className="mac-segment"
                          onClick={() => {
                            if (!inSession) patch({ sessionTarget: 'session' })
                          }}
                        >
                          {t('jobs.task.session.named')}
                        </button>
                      </div>
                      {inSession ? (
                        <SessionPicker
                          value={form.targetSessionKey}
                          onChange={(targetSessionKey) => patch({ targetSessionKey })}
                        />
                      ) : null}
                    </div>
                  </div>

                  <div className="jobs-form__row jobs-form__row--top">
                    <span className="mac-label">{t('jobs.task.elevated')}</span>
                    <div className="jobs-elevated" data-on={form.elevated}>
                      <Switch
                        checked={form.elevated}
                        onCheckedChange={(elevated) => patch({ elevated })}
                        aria-label={t('jobs.task.elevated')}
                      />
                      <p>
                        <ShieldAlert className="size-3.5" strokeWidth={2} aria-hidden />
                        {t('jobs.task.elevated.help')}
                      </p>
                    </div>
                  </div>
                </>
              ) : null}
            </div>
          </Section>

          <details className="jobs-disclosure">
            <summary>{t('jobs.section.delivery')}</summary>
            <div className="jobs-form__stack">
              <div className="jobs-form__row">
                <span className="mac-label">{t('jobs.delivery.mode')}</span>
                <select
                  className="mac-select"
                  aria-label={t('jobs.delivery.mode')}
                  value={form.deliveryMode}
                  onChange={(e) => patch({ deliveryMode: e.target.value as DeliveryMode })}
                >
                  {DELIVERY_MODES.map((m) => (
                    <option key={m.value} value={m.value}>
                      {t(m.label)}
                    </option>
                  ))}
                </select>
              </div>

              {form.deliveryMode === 'announce' ? (
                <ChannelFields
                  channel={form.deliveryChannel}
                  to={form.deliveryTo}
                  account={form.deliveryAccount}
                  targets={targetsForChannel(deliveryTargets, form.deliveryChannel)}
                  onChange={(next) =>
                    patch({
                      deliveryChannel: next.channel,
                      deliveryTo: next.to,
                      deliveryAccount: next.account,
                    })
                  }
                />
              ) : null}

              {form.deliveryMode === 'webhook' ? (
                <WebhookFields
                  url={form.deliveryWebhookUrl}
                  token={form.deliveryWebhookToken}
                  onChange={(next) =>
                    patch({ deliveryWebhookUrl: next.url, deliveryWebhookToken: next.token })
                  }
                />
              ) : null}

              {form.deliveryMode === 'announce' || form.deliveryMode === 'webhook' ? (
                <div className="jobs-form__row">
                  <span className="mac-label">{t('jobs.delivery.bestEffort')}</span>
                  <div className="jobs-form__inline">
                    <Switch
                      checked={form.deliveryBestEffort}
                      onCheckedChange={(deliveryBestEffort) => patch({ deliveryBestEffort })}
                      aria-label={t('jobs.delivery.bestEffort')}
                    />
                    <span className="mac-help">{t('jobs.delivery.bestEffort.help')}</span>
                  </div>
                </div>
              ) : null}

              <div className="jobs-form__row">
                <span className="mac-label">{t('jobs.delivery.failure')}</span>
                <select
                  className="mac-select"
                  aria-label={t('jobs.delivery.failure')}
                  value={form.fdMode}
                  onChange={(e) => patch({ fdMode: e.target.value as FailureDestMode })}
                >
                  {FAILURE_MODES.map((m) => (
                    <option key={m.value} value={m.value}>
                      {t(m.label)}
                    </option>
                  ))}
                </select>
              </div>

              {form.fdMode === 'channel' ? (
                <ChannelFields
                  channel={form.fdChannel}
                  to={form.fdTo}
                  account={form.fdAccount}
                  targets={targetsForChannel(deliveryTargets, form.fdChannel)}
                  onChange={(next) =>
                    patch({ fdChannel: next.channel, fdTo: next.to, fdAccount: next.account })
                  }
                />
              ) : null}
              {form.fdMode === 'webhook' ? (
                <WebhookFields
                  url={form.fdWebhookUrl}
                  token={form.fdWebhookToken}
                  onChange={(next) => patch({ fdWebhookUrl: next.url, fdWebhookToken: next.token })}
                />
              ) : null}
            </div>
          </details>

          <details className="jobs-disclosure">
            <summary>{t('jobs.section.advanced')}</summary>
            <div className="jobs-form__stack">
              <div className="jobs-form__row">
                <label className="mac-label" htmlFor={`${titleId}-agent`}>
                  {t('jobs.task.agent')}
                </label>
                <div className="mac-field">
                  <input
                    id={`${titleId}-agent`}
                    className="mac-input"
                    data-mono="true"
                    type="text"
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="main"
                    value={form.agentId}
                    onChange={(e) => patch({ agentId: e.target.value })}
                  />
                  <span className="mac-help">{t('jobs.advanced.agent.help')}</span>
                </div>
              </div>
              <div className="jobs-form__row">
                <span className="mac-label">{t('jobs.advanced.wake')}</span>
                <select
                  className="mac-select"
                  aria-label={t('jobs.advanced.wake')}
                  value={form.wakeMode}
                  onChange={(e) => patch({ wakeMode: e.target.value })}
                >
                  <option value="now">{t('jobs.advanced.wake.now')}</option>
                  <option value="next-heartbeat">{t('jobs.advanced.wake.heartbeat')}</option>
                </select>
              </div>
            </div>
          </details>
        </div>

        <footer className="jobs-sheet__foot">
          {shownError ? (
            <p className="jobs-sheet__error" role="alert">
              <TriangleAlert className="size-3.5" strokeWidth={2} aria-hidden />
              {shownError}
            </p>
          ) : (
            <span />
          )}
          <div className="jobs-sheet__buttons">
            <Button disabled={saving} onClick={onCancel}>
              {t('jobs.sheet.cancel')}
            </Button>
            <Button type="submit" variant="primary" disabled={saving}>
              {saving
                ? t('jobs.sheet.saving')
                : isEdit
                  ? t('jobs.sheet.save')
                  : t('jobs.sheet.create')}
            </Button>
          </div>
        </footer>
      </form>
    </ModalShell>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="jobs-section">
      <h3 className="jobs-section__title">{title}</h3>
      {children}
    </section>
  )
}

function Warning({ children }: { children: React.ReactNode }) {
  return (
    <p className="jobs-warning">
      <TriangleAlert className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
      <span>{children}</span>
    </p>
  )
}

/**
 * Pick the session an agent task continues. The sidebar's session list is the
 * source, so the choice is by title, not by pasting a key; a key that is not
 * in the list (another agent's, a channel session) stays typeable.
 */
function SessionPicker({ value, onChange }: { value: string; onChange: (key: string) => void }) {
  const { rows } = useSessions()
  const [manual, setManual] = useState(() => Boolean(value) && !rows.some((r) => r.key === value))
  const known = rows.some((r) => r.key === value)

  if (manual || (rows.length === 0 && value)) {
    return (
      <input
        className="mac-input"
        data-mono="true"
        type="text"
        autoComplete="off"
        spellCheck={false}
        placeholder={t('jobs.task.session.key.placeholder')}
        aria-label={t('jobs.task.session.key')}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  }
  return (
    <select
      className="mac-select"
      aria-label={t('jobs.task.session.pick')}
      value={known ? value : ''}
      onChange={(e) => {
        if (e.target.value === MANUAL) {
          setManual(true)
          onChange('')
          return
        }
        onChange(e.target.value)
      }}
    >
      <option value="">{t('jobs.task.session.pick')}</option>
      {rows.map((r) => (
        <option key={r.key} value={r.key}>
          {r.title}
        </option>
      ))}
      <option value={MANUAL}>{t('jobs.delivery.recipient.manual')}</option>
    </select>
  )
}

function ChannelFields({
  channel,
  to,
  account,
  targets,
  onChange,
}: {
  channel: string
  to: string
  account: string
  targets: DeliveryTarget[]
  onChange: (next: { channel: string; to: string; account: string }) => void
}) {
  const [manual, setManual] = useState(() => Boolean(to) && !targets.some((x) => x.id === to))
  const asPicker = targets.length > 0 && !manual
  return (
    <>
      <div className="jobs-form__row">
        <span className="mac-label">{t('jobs.delivery.channel')}</span>
        <input
          className="mac-input"
          type="text"
          autoComplete="off"
          placeholder={t('jobs.delivery.channel.placeholder')}
          aria-label={t('jobs.delivery.channel')}
          value={channel}
          onChange={(e) => onChange({ channel: e.target.value, to, account })}
        />
      </div>
      <div className="jobs-form__row">
        <span className="mac-label">{t('jobs.delivery.recipient')}</span>
        {asPicker ? (
          <select
            className="mac-select"
            aria-label={t('jobs.delivery.recipient')}
            value={to}
            onChange={(e) => {
              if (e.target.value === MANUAL) {
                setManual(true)
                onChange({ channel, to: '', account })
                return
              }
              onChange({ channel, to: e.target.value, account })
            }}
          >
            <option value="">{t('jobs.delivery.recipient.pick')}</option>
            {targets.map((x) => (
              <option key={x.id} value={x.id}>
                {x.label}
              </option>
            ))}
            <option value={MANUAL}>{t('jobs.delivery.recipient.manual')}</option>
          </select>
        ) : (
          <input
            className="mac-input"
            data-mono="true"
            type="text"
            autoComplete="off"
            placeholder={t('jobs.delivery.recipient.placeholder')}
            aria-label={t('jobs.delivery.recipient')}
            value={to}
            onChange={(e) => onChange({ channel, to: e.target.value, account })}
          />
        )}
      </div>
      <div className="jobs-form__row">
        <span className="mac-label">{t('jobs.delivery.account')}</span>
        <input
          className="mac-input"
          type="text"
          autoComplete="off"
          aria-label={t('jobs.delivery.account')}
          value={account}
          onChange={(e) => onChange({ channel, to, account: e.target.value })}
        />
      </div>
    </>
  )
}

function WebhookFields({
  url,
  token,
  onChange,
}: {
  url: string
  token: string
  onChange: (next: { url: string; token: string }) => void
}) {
  return (
    <>
      <div className="jobs-form__row">
        <span className="mac-label">{t('jobs.delivery.webhookUrl')}</span>
        <input
          className="mac-input"
          data-mono="true"
          type="url"
          autoComplete="off"
          spellCheck={false}
          placeholder={t('jobs.delivery.webhookUrl.placeholder')}
          aria-label={t('jobs.delivery.webhookUrl')}
          value={url}
          onChange={(e) => onChange({ url: e.target.value, token })}
        />
      </div>
      <div className="jobs-form__row">
        <span className="mac-label">{t('jobs.delivery.webhookToken')}</span>
        <input
          className="mac-input"
          type="password"
          autoComplete="off"
          placeholder={t('jobs.delivery.webhookToken.placeholder')}
          aria-label={t('jobs.delivery.webhookToken')}
          value={token}
          onChange={(e) => onChange({ url, token: e.target.value })}
        />
      </div>
    </>
  )
}
