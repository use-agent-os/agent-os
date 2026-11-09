import { Loader2, X } from 'lucide-react'
import { useId, useMemo, useState, type ReactNode } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webPagesWork'
import type { Gate } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  buildSavePayload,
  explainCron,
  humanCountdown,
  humanTime,
  nextRuns,
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
  type ScheduleKind,
  type SessionTarget,
} from '@/views/cron/logic'
import { Button } from '@/web/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/web/components/ui/sheet'
import { Switch } from '@/web/components/ui/switch'

// The create / edit panel for one automation (task 8.5). All validation and
// payload assembly is views/cron/logic (seedForm, resolveTarget,
// buildSavePayload); this file renders the form around it.

export type SaveRequest = Extract<SaveBuild, { ok: true }>

const inputClass =
  'h-9 w-full rounded-md border border-border-control bg-surface-0 px-3 text-ui touch:h-11 max-md:text-base disabled:opacity-60'

/** Sentinel option value: "the recipient I want is not in this list". */
const MANUAL_RECIPIENT = '__manual__'

function presets() {
  return [
    { cron: '*/5 * * * *', label: t('webPagesWork.presetEvery5') },
    { cron: '0 * * * *', label: t('webPagesWork.presetHourly') },
    { cron: '0 9 * * 1-5', label: t('webPagesWork.presetWeekdays') },
    { cron: '0 0 * * 0', label: t('webPagesWork.presetSundays') },
  ]
}

function scheduleKinds(): { value: ScheduleKind; label: string }[] {
  return [
    { value: 'cron', label: t('webPagesWork.scheduleCron') },
    { value: 'every', label: t('webPagesWork.scheduleEvery') },
    { value: 'at', label: t('webPagesWork.scheduleAt') },
  ]
}

function payloadKinds(): { value: PayloadKind; label: string }[] {
  return [
    { value: 'reminder', label: t('webPagesWork.payloadReminder') },
    { value: 'script', label: t('webPagesWork.payloadScript') },
    { value: 'agent_turn', label: t('webPagesWork.payloadAgentTurn') },
    { value: 'system_event', label: t('webPagesWork.payloadSystemEvent') },
  ]
}

function sessionTargets(): { value: SessionTarget; label: string }[] {
  return [
    { value: 'main', label: t('webPagesWork.targetMain') },
    { value: 'current', label: t('webPagesWork.targetCurrent') },
    { value: 'isolated', label: t('webPagesWork.targetIsolated') },
    { value: 'session', label: t('webPagesWork.targetSession') },
  ]
}

function deliveryModes(): { value: DeliveryMode; label: string }[] {
  return [
    { value: '', label: t('webPagesWork.deliveryDefault') },
    { value: 'none', label: t('webPagesWork.deliveryNone') },
    { value: 'announce', label: t('webPagesWork.deliveryAnnounce') },
    { value: 'webhook', label: t('webPagesWork.deliveryWebhook') },
  ]
}

function failureModes(): { value: FailureDestMode; label: string }[] {
  return [
    { value: '', label: t('webPagesWork.failureOff') },
    { value: 'channel', label: t('webPagesWork.failureChannel') },
    { value: 'webhook', label: t('webPagesWork.failureWebhook') },
  ]
}

function wakeModes(): { value: string; label: string }[] {
  return [
    { value: 'now', label: t('webPagesWork.wakeNow') },
    { value: 'next-heartbeat', label: t('webPagesWork.wakeHeartbeat') },
  ]
}

/** A labelled control; `hint` sits under it. */
function Field({
  label,
  hint,
  children,
  id,
}: {
  label: string
  hint?: ReactNode
  children: ReactNode
  id: string
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="font-medium">
        {label}
      </label>
      {children}
      {hint ? <span className="text-small text-text-muted">{hint}</span> : null}
    </div>
  )
}

/** The plain-language reading of a cron expression and its next three runs. */
export function CronExplain({ expr }: { expr: string }) {
  const trimmed = expr.trim()
  const parsed = useMemo(() => (trimmed ? parseCron(trimmed) : null), [trimmed])
  const upcoming = useMemo(() => (parsed ? nextRuns(parsed, 3) : []), [parsed])
  if (!trimmed) {
    return <p className="text-small text-text-muted">{t('webPagesWork.explainPrompt')}</p>
  }
  if (!parsed) {
    return (
      <p role="status" className="text-small text-danger">
        {t('webPagesWork.explainInvalid')}
      </p>
    )
  }
  return (
    <div role="status" className="rounded-md bg-surface-1 px-3 py-2 text-small">
      <p className="font-medium">{explainCron(trimmed) || t('webPagesWork.explainCustom')}</p>
      {upcoming.length ? (
        <>
          <p className="mt-1 text-text-muted">{t('webPagesWork.nextRuns')}</p>
          <ul role="list" className="tabular-nums">
            {upcoming.map((d) => (
              <li key={d.getTime()} className="flex gap-2">
                <span>{humanCountdown(d)}</span>
                <span className="text-text-muted">{humanTime(d)}</span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  )
}

/**
 * The delivery recipient: a picker when the gateway can list the channel's
 * chats (channels.deliveryTargets), a text box otherwise. A free-text box is
 * how a session key once got saved as a chat id, so offer the real chats.
 */
function RecipientField({
  value,
  targets,
  placeholder,
  onChange,
}: {
  value: string
  targets: DeliveryTarget[]
  placeholder: string
  onChange: (next: string) => void
}) {
  const id = useId()
  // An id outside the list (a group chat, an older job) has to stay editable.
  const [manual, setManual] = useState(() => !!value && !targets.some((x) => x.id === value))
  const asPicker = targets.length > 0 && !manual
  return (
    <Field id={id} label={t('webPagesWork.fieldRecipient')}>
      {asPicker ? (
        <select
          id={id}
          className={inputClass}
          value={value}
          onChange={(e) => {
            if (e.target.value === MANUAL_RECIPIENT) {
              setManual(true)
              onChange('')
              return
            }
            onChange(e.target.value)
          }}
        >
          <option value="">{t('webPagesWork.recipientSelect')}</option>
          {targets.map((target) => (
            <option key={target.id} value={target.id}>
              {target.label}
            </option>
          ))}
          <option value={MANUAL_RECIPIENT}>{t('webPagesWork.recipientManual')}</option>
        </select>
      ) : (
        <input
          id={id}
          className={inputClass}
          placeholder={placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </Field>
  )
}

export function AutomationEditor({
  job,
  template,
  activeSessionKey,
  deliveryTargets,
  saving,
  saveError,
  control,
  onCancel,
  onSubmit,
}: {
  job: RawJob | null
  template: Partial<RawJob> | null
  activeSessionKey: string
  deliveryTargets?: DeliveryTargetMap
  saving: boolean
  saveError: string | null
  control: Gate
  onCancel: () => void
  onSubmit: (request: SaveRequest) => void
}) {
  const [form, setForm] = useState<CronForm>(() => seedForm(job, template, activeSessionKey))
  const [invalid, setInvalid] = useState<string | null>(null)
  const ids = {
    name: useId(),
    kind: useId(),
    cron: useId(),
    every: useId(),
    at: useId(),
    tz: useId(),
    payload: useId(),
    agent: useId(),
    target: useId(),
    targetKey: useId(),
    message: useId(),
    script: useId(),
    args: useId(),
    workdir: useId(),
    wake: useId(),
    delivery: useId(),
    channel: useId(),
    account: useId(),
    webhook: useId(),
    token: useId(),
    fd: useId(),
    fdChannel: useId(),
    fdAccount: useId(),
    fdWebhook: useId(),
    fdToken: useId(),
    reason: useId(),
  }
  const isEdit = !!job

  const set = <K extends keyof CronForm>(key: K, value: CronForm[K]) =>
    setForm((f) => ({ ...f, [key]: value }))

  const targetRes = resolveTarget(form.payloadKind, form.sessionTarget, activeSessionKey)
  // A script job's script is the job; an agent turn may add one as a pre-run
  // collector. No other kind runs a file.
  const isScriptJob = form.payloadKind === 'script'
  const canRunScript = isScriptJob || form.payloadKind === 'agent_turn'

  const submit = () => {
    const effective: CronForm = { ...form, sessionTarget: targetRes.target }
    const build = buildSavePayload(effective, job, activeSessionKey)
    if (!build.ok) {
      setInvalid(build.error)
      return
    }
    setInvalid(null)
    onSubmit(build)
  }

  const error = invalid ?? saveError

  return (
    <Sheet open onOpenChange={(open) => !open && !saving && onCancel()}>
      <SheetContent side="right" className="w-[min(100vw,36rem)]">
        <form
          noValidate
          className="flex min-h-0 flex-1 flex-col"
          onSubmit={(e) => {
            e.preventDefault()
            submit()
          }}
        >
          <div className="flex items-start gap-2 border-b border-border px-4 py-3">
            <div className="min-w-0 flex-1">
              <SheetTitle className="text-base font-semibold">
                {isEdit ? t('webPagesWork.editTitle') : t('webPagesWork.createTitle')}
              </SheetTitle>
              <SheetDescription className="text-small text-text-muted">
                {t('webPagesWork.editorBody')}
              </SheetDescription>
            </div>
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label={t('webPagesWork.close')}
              disabled={saving}
              onClick={onCancel}
            >
              <X />
            </Button>
          </div>

          <div className="scroll-contained flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-4 py-4">
            <Field id={ids.name} label={t('webPagesWork.fieldName')}>
              <input
                id={ids.name}
                autoFocus
                autoComplete="off"
                className={inputClass}
                placeholder={t('webPagesWork.fieldNamePlaceholder')}
                value={form.name}
                onChange={(e) => set('name', e.target.value)}
              />
            </Field>

            <Field id={ids.kind} label={t('webPagesWork.fieldScheduleType')}>
              <select
                id={ids.kind}
                className={inputClass}
                value={form.scheduleKind}
                onChange={(e) => set('scheduleKind', e.target.value as ScheduleKind)}
              >
                {scheduleKinds().map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </Field>

            {form.scheduleKind === 'cron' ? (
              <div className="flex flex-col gap-2">
                <Field id={ids.cron} label={t('webPagesWork.fieldCron')}>
                  <input
                    id={ids.cron}
                    autoComplete="off"
                    spellCheck={false}
                    className={cn(inputClass, 'font-mono')}
                    placeholder="0 9 * * 1-5"
                    value={form.cron}
                    onChange={(e) => set('cron', e.target.value)}
                  />
                </Field>
                <CronExplain expr={form.cron} />
                <div
                  role="group"
                  aria-label={t('webPagesWork.presets')}
                  className="flex flex-wrap gap-1.5"
                >
                  {presets().map((p) => (
                    <Button
                      key={p.cron}
                      size="sm"
                      variant="outline"
                      onClick={() => set('cron', p.cron)}
                    >
                      {p.label}
                    </Button>
                  ))}
                </div>
              </div>
            ) : null}

            {form.scheduleKind === 'every' ? (
              <Field id={ids.every} label={t('webPagesWork.fieldInterval')}>
                <input
                  id={ids.every}
                  type="number"
                  min="1"
                  inputMode="numeric"
                  className={inputClass}
                  placeholder="60"
                  value={form.every}
                  onChange={(e) => set('every', e.target.value)}
                />
              </Field>
            ) : null}

            {form.scheduleKind === 'at' ? (
              <Field id={ids.at} label={t('webPagesWork.fieldAt')}>
                <input
                  id={ids.at}
                  className={cn(inputClass, 'font-mono')}
                  placeholder="2026-05-18T09:00:00+08:00"
                  value={form.at}
                  onChange={(e) => set('at', e.target.value)}
                />
              </Field>
            ) : null}

            <Field
              id={ids.tz}
              label={t('webPagesWork.fieldTimezone')}
              hint={t('webPagesWork.fieldTimezoneHint')}
            >
              <input
                id={ids.tz}
                autoComplete="off"
                spellCheck={false}
                className={cn(inputClass, 'font-mono')}
                placeholder={t('webPagesWork.fieldTimezonePlaceholder')}
                value={form.tz}
                onChange={(e) => set('tz', e.target.value)}
              />
            </Field>

            <Field id={ids.payload} label={t('webPagesWork.fieldPayload')}>
              <select
                id={ids.payload}
                className={inputClass}
                value={form.payloadKind}
                onChange={(e) => set('payloadKind', e.target.value as PayloadKind)}
              >
                {payloadKinds().map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </Field>

            <div className="grid gap-4 sm:grid-cols-2">
              <Field id={ids.agent} label={t('webPagesWork.fieldAgent')}>
                <input
                  id={ids.agent}
                  className={inputClass}
                  placeholder={t('webPagesWork.fieldAgentPlaceholder')}
                  value={form.agentId}
                  onChange={(e) => set('agentId', e.target.value)}
                />
              </Field>
              <Field
                id={ids.target}
                label={t('webPagesWork.fieldTarget')}
                hint={targetRes.locked ? t('webPagesWork.targetLocked') : undefined}
              >
                <select
                  id={ids.target}
                  className={inputClass}
                  value={targetRes.target}
                  disabled={targetRes.locked}
                  onChange={(e) => set('sessionTarget', e.target.value as SessionTarget)}
                >
                  {sessionTargets().map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </Field>
            </div>

            {targetRes.showTargetSessionRow ? (
              <Field
                id={ids.targetKey}
                label={
                  targetRes.target === 'current'
                    ? t('webPagesWork.fieldCurrentSession')
                    : t('webPagesWork.fieldNamedSession')
                }
              >
                <input
                  id={ids.targetKey}
                  className={cn(inputClass, 'font-mono')}
                  placeholder={t('webPagesWork.fieldSessionPlaceholder')}
                  value={form.targetSessionKey}
                  onChange={(e) => set('targetSessionKey', e.target.value)}
                />
              </Field>
            ) : null}

            {isScriptJob ? null : (
              <Field id={ids.message} label={targetRes.messageLabel}>
                <textarea
                  id={ids.message}
                  rows={4}
                  className={cn(inputClass, 'h-auto py-2')}
                  placeholder={t('webPagesWork.fieldMessagePlaceholder')}
                  value={form.message}
                  onChange={(e) => set('message', e.target.value)}
                />
              </Field>
            )}

            {canRunScript ? (
              <>
                <Field
                  id={ids.script}
                  label={
                    isScriptJob ? t('webPagesWork.fieldScript') : t('webPagesWork.fieldPreScript')
                  }
                  hint={
                    isScriptJob
                      ? t('webPagesWork.scriptHintDelivered')
                      : t('webPagesWork.scriptHintContext')
                  }
                >
                  <input
                    id={ids.script}
                    autoComplete="off"
                    spellCheck={false}
                    className={cn(inputClass, 'font-mono')}
                    placeholder={isScriptJob ? 'watch-memory.sh' : 'watch-rss.py'}
                    value={form.script}
                    onChange={(e) => set('script', e.target.value)}
                  />
                </Field>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field
                    id={ids.args}
                    label={t('webPagesWork.fieldScriptArgs')}
                    hint={t('webPagesWork.scriptArgsHint')}
                  >
                    <input
                      id={ids.args}
                      autoComplete="off"
                      spellCheck={false}
                      className={cn(inputClass, 'font-mono')}
                      value={form.scriptArgs}
                      onChange={(e) => set('scriptArgs', e.target.value)}
                    />
                  </Field>
                  <Field id={ids.workdir} label={t('webPagesWork.fieldWorkdir')}>
                    <input
                      id={ids.workdir}
                      autoComplete="off"
                      spellCheck={false}
                      className={cn(inputClass, 'font-mono')}
                      placeholder={t('webPagesWork.fieldWorkdirPlaceholder')}
                      value={form.workdir}
                      onChange={(e) => set('workdir', e.target.value)}
                    />
                  </Field>
                </div>
              </>
            ) : null}

            {isScriptJob || (canRunScript && form.script.trim()) ? (
              <p className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-small">
                {t('webPagesWork.scriptWarning')}
              </p>
            ) : null}

            {form.payloadKind === 'agent_turn' ? (
              <div className="flex flex-col gap-1 rounded-md border border-danger/40 bg-danger/10 px-3 py-2">
                <label className="flex items-center gap-2 font-medium">
                  <input
                    type="checkbox"
                    className="size-4"
                    checked={form.elevated}
                    onChange={(e) => set('elevated', e.target.checked)}
                  />
                  {t('webPagesWork.fieldElevated')}
                </label>
                <p className="text-small">{t('webPagesWork.elevatedWarning')}</p>
              </div>
            ) : null}

            <details className="rounded-md border border-border px-3 py-2">
              <summary className="target cursor-pointer font-medium">
                {t('webPagesWork.deliverySection')}
              </summary>
              <div className="mt-3 flex flex-col gap-4">
                <Field id={ids.wake} label={t('webPagesWork.fieldWake')}>
                  <select
                    id={ids.wake}
                    className={inputClass}
                    value={form.wakeMode}
                    onChange={(e) => set('wakeMode', e.target.value)}
                  >
                    {wakeModes().map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field id={ids.delivery} label={t('webPagesWork.fieldDelivery')}>
                  <select
                    id={ids.delivery}
                    className={inputClass}
                    value={form.deliveryMode}
                    onChange={(e) => set('deliveryMode', e.target.value as DeliveryMode)}
                  >
                    {deliveryModes().map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </Field>

                {form.deliveryMode === 'announce' ? (
                  <>
                    <Field id={ids.channel} label={t('webPagesWork.fieldChannel')}>
                      <input
                        id={ids.channel}
                        className={inputClass}
                        placeholder={t('webPagesWork.fieldChannelPlaceholder')}
                        value={form.deliveryChannel}
                        onChange={(e) => set('deliveryChannel', e.target.value)}
                      />
                    </Field>
                    <RecipientField
                      key={`to:${form.deliveryChannel}`}
                      value={form.deliveryTo}
                      targets={targetsForChannel(deliveryTargets, form.deliveryChannel)}
                      placeholder={t('webPagesWork.fieldChatPlaceholder')}
                      onChange={(next) => set('deliveryTo', next)}
                    />
                    <Field id={ids.account} label={t('webPagesWork.fieldAccount')}>
                      <input
                        id={ids.account}
                        className={inputClass}
                        value={form.deliveryAccount}
                        onChange={(e) => set('deliveryAccount', e.target.value)}
                      />
                    </Field>
                  </>
                ) : null}

                {form.deliveryMode === 'webhook' ? (
                  <>
                    <Field id={ids.webhook} label={t('webPagesWork.fieldWebhookUrl')}>
                      <input
                        id={ids.webhook}
                        type="url"
                        className={cn(inputClass, 'font-mono')}
                        placeholder={t('webPagesWork.fieldWebhookUrlPlaceholder')}
                        value={form.deliveryWebhookUrl}
                        onChange={(e) => set('deliveryWebhookUrl', e.target.value)}
                      />
                    </Field>
                    <Field id={ids.token} label={t('webPagesWork.fieldWebhookToken')}>
                      <input
                        id={ids.token}
                        type="password"
                        className={inputClass}
                        placeholder={t('webPagesWork.fieldWebhookTokenPlaceholder')}
                        value={form.deliveryWebhookToken}
                        onChange={(e) => set('deliveryWebhookToken', e.target.value)}
                      />
                    </Field>
                  </>
                ) : null}

                {form.deliveryMode === 'announce' || form.deliveryMode === 'webhook' ? (
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      className="size-4"
                      checked={form.deliveryBestEffort}
                      onChange={(e) => set('deliveryBestEffort', e.target.checked)}
                    />
                    {t('webPagesWork.fieldBestEffort')}
                  </label>
                ) : null}

                <Field
                  id={ids.fd}
                  label={t('webPagesWork.fieldFailure')}
                  hint={t('webPagesWork.fieldFailureHint')}
                >
                  <select
                    id={ids.fd}
                    className={inputClass}
                    value={form.fdMode}
                    onChange={(e) => set('fdMode', e.target.value as FailureDestMode)}
                  >
                    {failureModes().map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </Field>

                {form.fdMode === 'channel' ? (
                  <>
                    <Field id={ids.fdChannel} label={t('webPagesWork.fieldFailureChannel')}>
                      <input
                        id={ids.fdChannel}
                        className={inputClass}
                        placeholder={t('webPagesWork.fieldChannelPlaceholder')}
                        value={form.fdChannel}
                        onChange={(e) => set('fdChannel', e.target.value)}
                      />
                    </Field>
                    <RecipientField
                      key={`fd:${form.fdChannel}`}
                      value={form.fdTo}
                      targets={targetsForChannel(deliveryTargets, form.fdChannel)}
                      placeholder={t('webPagesWork.fieldChatPlaceholder')}
                      onChange={(next) => set('fdTo', next)}
                    />
                    <Field id={ids.fdAccount} label={t('webPagesWork.fieldAccount')}>
                      <input
                        id={ids.fdAccount}
                        className={inputClass}
                        value={form.fdAccount}
                        onChange={(e) => set('fdAccount', e.target.value)}
                      />
                    </Field>
                  </>
                ) : null}

                {form.fdMode === 'webhook' ? (
                  <>
                    <Field id={ids.fdWebhook} label={t('webPagesWork.fieldFailureWebhook')}>
                      <input
                        id={ids.fdWebhook}
                        type="url"
                        className={cn(inputClass, 'font-mono')}
                        placeholder={t('webPagesWork.fieldFailureWebhookPlaceholder')}
                        value={form.fdWebhookUrl}
                        onChange={(e) => set('fdWebhookUrl', e.target.value)}
                      />
                    </Field>
                    <Field id={ids.fdToken} label={t('webPagesWork.fieldWebhookToken')}>
                      <input
                        id={ids.fdToken}
                        type="password"
                        className={inputClass}
                        placeholder={t('webPagesWork.fieldWebhookTokenPlaceholder')}
                        value={form.fdWebhookToken}
                        onChange={(e) => set('fdWebhookToken', e.target.value)}
                      />
                    </Field>
                  </>
                ) : null}
              </div>
            </details>

            <label className="flex items-center gap-2 font-medium">
              <Switch checked={form.enabled} onCheckedChange={(on) => set('enabled', on)} />
              {t('webPagesWork.fieldEnabled')}
            </label>

            {error ? (
              <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2">
                {error}
              </p>
            ) : null}
          </div>

          <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border px-4 py-3">
            {control.reason ? (
              <span id={ids.reason} className="mr-auto text-small text-text-muted">
                {control.reason}
              </span>
            ) : null}
            <Button variant="outline" disabled={saving} onClick={onCancel}>
              {t('webPagesWork.cancel')}
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={saving || control.disabled}
              aria-describedby={control.reason ? ids.reason : undefined}
            >
              {saving ? <Loader2 className="animate-spin" /> : null}
              {isEdit ? t('webPagesWork.saveChanges') : t('webPagesWork.createAutomation')}
            </Button>
          </div>
        </form>
      </SheetContent>
    </Sheet>
  )
}
