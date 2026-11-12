import {
  AlertTriangle,
  CalendarClock,
  CalendarPlus,
  ChevronRight,
  Loader2,
  Pencil,
  Send,
  ShieldAlert,
  X,
  Zap,
} from 'lucide-react'
import { useId, useMemo, useState, type ReactNode } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webPagesWork'
import type { Gate } from '@/lib/disabled-reason'
import {
  buildDelivery,
  buildSavePayload,
  explainCron,
  humanCountdown,
  humanTime,
  nextRuns,
  parseCron,
  resolveTarget,
  seedForm,
  targetsForChannel,
  validateScriptPath,
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
import { Input, Select, Textarea } from '@/web/components/ui/input'
import { SectionHeader } from '@/web/components/ui/section-header'
import { SegmentedControl } from '@/web/components/ui/segmented-control'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/web/components/ui/sheet'
import { Switch } from '@/web/components/ui/switch'
import '@/web/styles/page-automations.css'

// The create / edit panel for one automation (task 8.5). All validation and
// payload assembly is views/cron/logic (seedForm, resolveTarget,
// buildSavePayload); this file renders the form around it.

export type SaveRequest = Extract<SaveBuild, { ok: true }>

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

/** A labelled control; `hint` sits under it, an inline `error` replaces the hint. */
function Field({
  label,
  hint,
  error,
  errorId,
  children,
  id,
}: {
  label: string
  hint?: ReactNode
  error?: string | null
  errorId?: string
  children: ReactNode
  id: string
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <label htmlFor={id} className="text-label">
        {label}
      </label>
      {children}
      {error ? (
        <FieldError id={errorId} text={error} />
      ) : hint ? (
        <span className="text-small text-pretty text-text-muted">{hint}</span>
      ) : null}
    </div>
  )
}

/** The validation message under the field it is about. */
function FieldError({ id, text }: { id?: string; text: string }) {
  return (
    <p
      id={id}
      role="alert"
      className="anim-fade-in flex items-start gap-1.5 text-small text-danger [&_svg]:mt-px [&_svg]:size-3.5 [&_svg]:shrink-0"
    >
      <AlertTriangle aria-hidden data-status-icon />
      {text}
    </p>
  )
}

/** One titled group of the form. */
function FormSection({
  id,
  title,
  icon,
  children,
}: {
  id: string
  title: string
  icon: ReactNode
  children: ReactNode
}) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-4">
      <SectionHeader id={id} as="h3" size="sm" icon={icon} title={title} />
      {children}
    </section>
  )
}

/**
 * The plain-language reading of a cron expression and its next three runs,
 * computed in the job's timezone (empty: UTC, as the scheduler runs it) and
 * shown in local time like the saved card.
 */
export function CronExplain({ expr, timeZone = '' }: { expr: string; timeZone?: string }) {
  const trimmed = expr.trim()
  const parsed = useMemo(() => (trimmed ? parseCron(trimmed) : null), [trimmed])
  const upcoming = useMemo(
    () => (parsed ? nextRuns(parsed, 3, undefined, timeZone) : []),
    [parsed, timeZone],
  )
  if (!trimmed) {
    return <p className="text-small text-text-muted">{t('webPagesWork.explainPrompt')}</p>
  }
  if (!parsed) {
    return (
      <p
        role="status"
        className="flex items-start gap-1.5 text-small text-danger [&_svg]:mt-px [&_svg]:size-3.5 [&_svg]:shrink-0"
      >
        <AlertTriangle aria-hidden data-status-icon />
        {t('webPagesWork.explainInvalid')}
      </p>
    )
  }
  return (
    <div role="status" className="am-explain anim-fade-in flex flex-col gap-3">
      <p className="flex items-center gap-2 font-medium [&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-accent">
        <CalendarClock aria-hidden />
        {explainCron(trimmed) || t('webPagesWork.explainCustom')}
      </p>
      {upcoming.length ? (
        <div className="flex flex-col gap-1.5">
          <p className="text-label text-text-muted">{t('webPagesWork.nextRuns')}</p>
          <ul role="list" className="am-explain-runs">
            {upcoming.map((d) => (
              <li key={d.getTime()}>
                <span className="font-medium">{humanCountdown(d)}</span>
                <span className="text-text-muted">{humanTime(d)}</span>
              </li>
            ))}
          </ul>
        </div>
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
        <Select
          id={id}
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
        </Select>
      ) : (
        <Input
          id={id}
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
  const [invalid, setInvalid] = useState<{ text: string; field: InvalidField | null } | null>(null)
  const ids = {
    name: useId(),
    kind: useId(),
    cron: useId(),
    every: useId(),
    at: useId(),
    tz: useId(),
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
    section: useId(),
    error: useId(),
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
      setInvalid({ text: build.error, field: fieldOfError(effective) })
      return
    }
    setInvalid(null)
    onSubmit(build)
  }

  // A validation error sits under the field it is about; anything else (a
  // delivery problem, a failed save) shows above the buttons.
  const invalidField = invalid?.field ?? null
  const error = invalidField ? saveError : (invalid?.text ?? saveError)
  const errorFor = (field: InvalidField) => (invalidField === field ? invalid!.text : null)
  const invalidProps = (field: InvalidField) =>
    invalidField === field ? { 'aria-invalid': true as const, 'aria-describedby': ids.error } : {}

  return (
    <Sheet open onOpenChange={(open) => !open && !saving && onCancel()}>
      <SheetContent side="right" className="w-[min(100vw,38rem)]">
        <form
          noValidate
          className="flex min-h-0 flex-1 flex-col"
          onSubmit={(e) => {
            e.preventDefault()
            submit()
          }}
        >
          <div className="flex items-start gap-3 border-b border-border px-5 py-4">
            <span
              aria-hidden
              className="grid size-9 shrink-0 place-items-center rounded-control border border-accent/20 bg-accent-tint text-accent shadow-highlight [&_svg]:size-4"
            >
              {isEdit ? <Pencil /> : <CalendarPlus />}
            </span>
            <div className="min-w-0 flex-1">
              <SheetTitle className="text-section">
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

          <div className="scroll-contained flex min-h-0 flex-1 flex-col gap-7 overflow-y-auto px-5 py-5">
            <Field
              id={ids.name}
              label={t('webPagesWork.fieldName')}
              error={errorFor('name')}
              errorId={ids.error}
            >
              <Input
                id={ids.name}
                autoFocus
                autoComplete="off"
                placeholder={t('webPagesWork.fieldNamePlaceholder')}
                value={form.name}
                onChange={(e) => set('name', e.target.value)}
                {...invalidProps('name')}
              />
            </Field>

            <FormSection
              id={ids.kind}
              icon={<CalendarClock />}
              title={t('webPagesWork.fieldScheduleType')}
            >
              <SegmentedControl
                aria-labelledby={ids.kind}
                value={form.scheduleKind}
                onChange={(kind) => set('scheduleKind', kind)}
                options={scheduleKinds()}
                size="sm"
                className="w-full sm:w-fit max-sm:[&>*]:!h-auto max-sm:[&>*]:min-h-8 max-sm:[&>*]:flex-1 max-sm:[&>*]:py-1 max-sm:[&>*]:text-center max-sm:[&>*]:leading-tight max-sm:[&>*]:!whitespace-normal"
              />

              {form.scheduleKind === 'cron' ? (
                <div className="flex flex-col gap-3">
                  <Field id={ids.cron} label={t('webPagesWork.fieldCron')}>
                    <Input
                      id={ids.cron}
                      autoComplete="off"
                      spellCheck={false}
                      className="font-mono"
                      placeholder="0 9 * * 1-5"
                      value={form.cron}
                      onChange={(e) => set('cron', e.target.value)}
                    />
                  </Field>
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
                        aria-pressed={form.cron.trim() === p.cron}
                        className="rounded-full aria-pressed:border-accent/40 aria-pressed:bg-accent-tint aria-pressed:text-accent"
                        onClick={() => set('cron', p.cron)}
                      >
                        {p.label}
                      </Button>
                    ))}
                  </div>
                  <CronExplain expr={form.cron} timeZone={form.tz} />
                </div>
              ) : null}

              {form.scheduleKind === 'every' ? (
                <Field
                  id={ids.every}
                  label={t('webPagesWork.fieldInterval')}
                  error={errorFor('every')}
                  errorId={ids.error}
                >
                  <Input
                    id={ids.every}
                    type="number"
                    min="1"
                    inputMode="numeric"
                    className="tabular-nums sm:max-w-48"
                    placeholder="60"
                    value={form.every}
                    onChange={(e) => set('every', e.target.value)}
                    {...invalidProps('every')}
                  />
                </Field>
              ) : null}

              {form.scheduleKind === 'at' ? (
                <Field
                  id={ids.at}
                  label={t('webPagesWork.fieldAt')}
                  error={errorFor('at')}
                  errorId={ids.error}
                >
                  <Input
                    id={ids.at}
                    className="font-mono"
                    placeholder="2026-05-18T09:00:00+08:00"
                    value={form.at}
                    onChange={(e) => set('at', e.target.value)}
                    {...invalidProps('at')}
                  />
                </Field>
              ) : null}

              <Field
                id={ids.tz}
                label={t('webPagesWork.fieldTimezone')}
                hint={t('webPagesWork.fieldTimezoneHint')}
              >
                <Input
                  id={ids.tz}
                  autoComplete="off"
                  spellCheck={false}
                  className="font-mono"
                  placeholder={t('webPagesWork.fieldTimezonePlaceholder')}
                  value={form.tz}
                  onChange={(e) => set('tz', e.target.value)}
                />
              </Field>
            </FormSection>

            <FormSection id={ids.section} icon={<Zap />} title={t('webPagesWork.fieldPayload')}>
              <Select
                aria-labelledby={ids.section}
                value={form.payloadKind}
                onChange={(e) => set('payloadKind', e.target.value as PayloadKind)}
              >
                {payloadKinds().map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </Select>

              <div className="grid gap-4 sm:grid-cols-2">
                <Field id={ids.agent} label={t('webPagesWork.fieldAgent')}>
                  <Input
                    id={ids.agent}
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
                  <Select
                    id={ids.target}
                    value={targetRes.target}
                    disabled={targetRes.locked}
                    onChange={(e) => set('sessionTarget', e.target.value as SessionTarget)}
                  >
                    {sessionTargets().map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </Select>
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
                  error={errorFor('targetKey')}
                  errorId={ids.error}
                >
                  <Input
                    id={ids.targetKey}
                    className="font-mono"
                    placeholder={t('webPagesWork.fieldSessionPlaceholder')}
                    value={form.targetSessionKey}
                    onChange={(e) => set('targetSessionKey', e.target.value)}
                    {...invalidProps('targetKey')}
                  />
                </Field>
              ) : null}

              {isScriptJob ? null : (
                <Field id={ids.message} label={targetRes.messageLabel}>
                  <Textarea
                    id={ids.message}
                    rows={4}
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
                    error={errorFor('script')}
                    errorId={ids.error}
                  >
                    <Input
                      id={ids.script}
                      autoComplete="off"
                      spellCheck={false}
                      className="font-mono"
                      placeholder={isScriptJob ? 'watch-memory.sh' : 'watch-rss.py'}
                      value={form.script}
                      onChange={(e) => set('script', e.target.value)}
                      {...invalidProps('script')}
                    />
                  </Field>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <Field
                      id={ids.args}
                      label={t('webPagesWork.fieldScriptArgs')}
                      hint={t('webPagesWork.scriptArgsHint')}
                    >
                      <Input
                        id={ids.args}
                        autoComplete="off"
                        spellCheck={false}
                        className="font-mono"
                        value={form.scriptArgs}
                        onChange={(e) => set('scriptArgs', e.target.value)}
                      />
                    </Field>
                    <Field id={ids.workdir} label={t('webPagesWork.fieldWorkdir')}>
                      <Input
                        id={ids.workdir}
                        autoComplete="off"
                        spellCheck={false}
                        className="font-mono"
                        placeholder={t('webPagesWork.fieldWorkdirPlaceholder')}
                        value={form.workdir}
                        onChange={(e) => set('workdir', e.target.value)}
                      />
                    </Field>
                  </div>
                </>
              ) : null}

              {isScriptJob || (canRunScript && form.script.trim()) ? (
                <p className="anim-fade-in flex items-start gap-2 rounded-card border border-danger/30 bg-danger/10 px-3 py-2.5 text-small">
                  <ShieldAlert
                    className="mt-px size-4 shrink-0 text-danger"
                    aria-hidden
                    data-status-icon
                  />
                  {t('webPagesWork.scriptWarning')}
                </p>
              ) : null}

              {form.payloadKind === 'agent_turn' ? (
                <div className="anim-fade-in flex flex-col gap-1.5 rounded-card border border-danger/30 bg-danger/10 px-3 py-2.5">
                  <label className="flex items-center gap-2 font-medium">
                    <input
                      type="checkbox"
                      className="am-check"
                      checked={form.elevated}
                      onChange={(e) => set('elevated', e.target.checked)}
                      {...invalidProps('elevated')}
                    />
                    {t('webPagesWork.fieldElevated')}
                  </label>
                  <p className="text-small">{t('webPagesWork.elevatedWarning')}</p>
                  {errorFor('elevated') ? (
                    <FieldError id={ids.error} text={errorFor('elevated')!} />
                  ) : null}
                </div>
              ) : null}
            </FormSection>

            <details className="am-details group rounded-card border border-border bg-surface-1 shadow-card">
              <summary className="press target flex cursor-pointer items-center gap-2 rounded-card px-4 py-3 font-medium hover:bg-surface-2">
                <Send className="size-4 shrink-0 text-text-muted" aria-hidden />
                <span className="flex-1">{t('webPagesWork.deliverySection')}</span>
                <ChevronRight className="am-chevron size-4 text-text-muted" aria-hidden />
              </summary>
              <div className="flex flex-col gap-4 border-t border-border px-4 pt-4 pb-4">
                <Field id={ids.wake} label={t('webPagesWork.fieldWake')}>
                  <Select
                    id={ids.wake}
                    value={form.wakeMode}
                    onChange={(e) => set('wakeMode', e.target.value)}
                  >
                    {wakeModes().map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field id={ids.delivery} label={t('webPagesWork.fieldDelivery')}>
                  <Select
                    id={ids.delivery}
                    value={form.deliveryMode}
                    onChange={(e) => set('deliveryMode', e.target.value as DeliveryMode)}
                  >
                    {deliveryModes().map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </Select>
                </Field>

                {form.deliveryMode === 'announce' ? (
                  <div className="grid gap-4 sm:grid-cols-2">
                    <Field id={ids.channel} label={t('webPagesWork.fieldChannel')}>
                      <Input
                        id={ids.channel}
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
                      <Input
                        id={ids.account}
                        value={form.deliveryAccount}
                        onChange={(e) => set('deliveryAccount', e.target.value)}
                      />
                    </Field>
                  </div>
                ) : null}

                {form.deliveryMode === 'webhook' ? (
                  <>
                    <Field id={ids.webhook} label={t('webPagesWork.fieldWebhookUrl')}>
                      <Input
                        id={ids.webhook}
                        type="url"
                        className="font-mono"
                        placeholder={t('webPagesWork.fieldWebhookUrlPlaceholder')}
                        value={form.deliveryWebhookUrl}
                        onChange={(e) => set('deliveryWebhookUrl', e.target.value)}
                      />
                    </Field>
                    <Field id={ids.token} label={t('webPagesWork.fieldWebhookToken')}>
                      <Input
                        id={ids.token}
                        type="password"
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
                      className="am-check"
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
                  <Select
                    id={ids.fd}
                    value={form.fdMode}
                    onChange={(e) => set('fdMode', e.target.value as FailureDestMode)}
                  >
                    {failureModes().map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </Select>
                </Field>

                {form.fdMode === 'channel' ? (
                  <div className="grid gap-4 sm:grid-cols-2">
                    <Field id={ids.fdChannel} label={t('webPagesWork.fieldFailureChannel')}>
                      <Input
                        id={ids.fdChannel}
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
                      <Input
                        id={ids.fdAccount}
                        value={form.fdAccount}
                        onChange={(e) => set('fdAccount', e.target.value)}
                      />
                    </Field>
                  </div>
                ) : null}

                {form.fdMode === 'webhook' ? (
                  <>
                    <Field id={ids.fdWebhook} label={t('webPagesWork.fieldFailureWebhook')}>
                      <Input
                        id={ids.fdWebhook}
                        type="url"
                        className="font-mono"
                        placeholder={t('webPagesWork.fieldFailureWebhookPlaceholder')}
                        value={form.fdWebhookUrl}
                        onChange={(e) => set('fdWebhookUrl', e.target.value)}
                      />
                    </Field>
                    <Field id={ids.fdToken} label={t('webPagesWork.fieldWebhookToken')}>
                      <Input
                        id={ids.fdToken}
                        type="password"
                        placeholder={t('webPagesWork.fieldWebhookTokenPlaceholder')}
                        value={form.fdWebhookToken}
                        onChange={(e) => set('fdWebhookToken', e.target.value)}
                      />
                    </Field>
                  </>
                ) : null}
              </div>
            </details>

            <label className="flex items-center justify-between gap-3 rounded-card border border-border bg-surface-1 px-4 py-3 font-medium shadow-card">
              {t('webPagesWork.fieldEnabled')}
              <Switch checked={form.enabled} onCheckedChange={(on) => set('enabled', on)} />
            </label>
          </div>

          <div className="flex flex-col gap-3 border-t border-border bg-surface-1 px-5 py-3">
            {error ? (
              <p
                role="alert"
                className="anim-fade-in flex items-start gap-2 rounded-control border border-danger/30 bg-danger/10 px-3 py-2"
              >
                <AlertTriangle
                  className="mt-0.5 size-4 shrink-0 text-danger"
                  aria-hidden
                  data-status-icon
                />
                {error}
              </p>
            ) : null}
            <div className="flex flex-wrap items-center justify-end gap-2">
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
          </div>
        </form>
      </SheetContent>
    </Sheet>
  )
}

type InvalidField = 'name' | 'every' | 'at' | 'targetKey' | 'script' | 'elevated'

/**
 * Which field a buildSavePayload error is about, or null for a form-level one
 * (delivery). Follows buildSavePayload's own order of checks on the same form.
 */
function fieldOfError(form: CronForm): InvalidField | null {
  if (!form.name.trim()) return 'name'
  const kind = form.payloadKind
  if (
    (kind === 'script' || (kind === 'agent_turn' && form.script.trim())) &&
    validateScriptPath(form.script)
  )
    return 'script'
  if (form.scheduleKind === 'every') {
    const seconds = Number(form.every)
    if (!Number.isInteger(seconds) || seconds < 1) return 'every'
  }
  if (form.scheduleKind === 'at' && !form.at.trim()) return 'at'
  if (form.elevated && kind !== 'agent_turn') return 'elevated'
  if (!buildDelivery(form).ok) return null
  if (form.sessionTarget === 'current' || form.sessionTarget === 'session') return 'targetKey'
  return null
}
