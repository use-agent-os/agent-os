import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Eye, EyeOff, Loader2, Lock, Plus, RefreshCw, Settings2 } from 'lucide-react'
import { useEffect, useId, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webSetAccess'
import { gate, requireConnection, type Gate } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  channelDisplay,
  channelStats,
  isAccessLocked,
  mergeChannels,
  senderLabel,
  senderMeta,
  sortChannels,
  statusHint,
  type AccessAccount,
  type ChannelAccess,
  type MergedChannel,
  type RawChannel,
} from '@/views/channels/logic'
import { relTime } from '@/views/overview/logic'
import {
  loadSettingsSnapshot,
  SETTINGS_SNAPSHOT_QUERY_KEY,
  type SettingsSnapshot,
} from '@/views/settings/snapshot'
import {
  readScopedFields,
  validateScopedRequiredFields,
  type FieldSpec,
  type ScopedField,
} from '@/views/setup/logic'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/web/components/ui/alert-dialog'
import { Button } from '@/web/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { Switch } from '@/web/components/ui/switch'
import { ChannelIcon } from '@/web/shell/channel-icon'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useRestartNotice } from '../config-editor'
import { useRegisterFields, useReveal } from '../fields'

// Settings › Channels (overhaul-control-ui 7.4, Appendix A "Channels"):
// adapter status and stats, Telegram pairing, and the channel setup dialog.

const CHANNELS_KEY = ['channels'] as const
const POLL_MS = 5_000

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function useWriteGate(): Gate {
  return gate(requireConnection(useDisplayedConnection()))
}

function GateReason({ gate: g, className }: { gate: Gate; className?: string }) {
  return g.reason ? <p className={cn('text-small text-text-muted', className)}>{g.reason}</p> : null
}

const TONE_CLASS = { ok: 'text-ok', danger: 'text-danger', off: 'text-text-muted' } as const

// ── Pairing ─────────────────────────────────────────────────────────────────

interface PairingTarget {
  channel: string
  senderId: string
  label: string
}

function PersonRow({
  item,
  channel,
  pending,
  write,
  locked,
  onRevoke,
}: {
  item: AccessAccount
  channel: string
  pending: boolean
  write: Gate
  locked: boolean
  onRevoke: (target: PairingTarget) => void
}) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const senderId = String(item.sender_id ?? '')
  const label = senderLabel(item)
  const resolve = useMutation({
    mutationFn: (approved: boolean) =>
      rpc.call(approved ? 'channels.pairing.approve' : 'channels.pairing.deny', {
        channel,
        senderId,
      }),
    onMutate: () => setError(null),
    onSuccess: (_data, approved) => {
      toast.success(
        approved
          ? t('webSetAccess.chPairedToast', { name: label })
          : t('webSetAccess.chDeniedToast', { name: label }),
      )
      void queryClient.invalidateQueries({ queryKey: CHANNELS_KEY })
    },
    onError: (e) => setError(t('webSetAccess.chPairingFailed', { message: errorMessage(e) })),
  })
  const busy = resolve.isPending
  return (
    <li className="flex flex-col gap-1 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <p className="font-medium">{label}</p>
          <p className="truncate text-small text-text-muted">{senderMeta(item)}</p>
        </div>
        {pending ? (
          <>
            <Button
              size="sm"
              variant="primary"
              disabled={write.disabled || locked || busy}
              onClick={() => resolve.mutate(true)}
            >
              {busy && resolve.variables ? <Loader2 className="animate-spin" /> : null}
              {t('webSetAccess.chApprove')}
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={write.disabled || busy}
              onClick={() => resolve.mutate(false)}
            >
              {t('webSetAccess.chDeny')}
            </Button>
          </>
        ) : (
          <Button
            size="sm"
            variant="outline"
            disabled={write.disabled}
            onClick={() => onRevoke({ channel, senderId, label })}
          >
            {t('webSetAccess.chRevoke')}
          </Button>
        )}
      </div>
      {error ? (
        <p role="alert" className="text-small text-danger">
          {error}
        </p>
      ) : null}
    </li>
  )
}

function AccessPanel({
  channel,
  write,
  onRevoke,
}: {
  channel: MergedChannel
  write: Gate
  onRevoke: (target: PairingTarget) => void
}) {
  // Only Telegram has pairing: one binary state, pending or paired.
  const access: ChannelAccess | null = channel.access
  if (!access || channel.type !== 'telegram') return null
  const name = String(channel.name || '')
  const pending = Array.isArray(access.pending) ? access.pending : []
  const paired = Array.isArray(access.paired) ? access.paired : []
  const locked = isAccessLocked(access.locked_until)
  const groups = access.groups_enabled
    ? t(
        access.group_mention_required
          ? 'webSetAccess.chGroupsMention'
          : 'webSetAccess.chGroupsEnabled',
        { count: access.group_chat_ids?.length ?? 0 },
      )
    : t('webSetAccess.chGroupsDisabled')

  const group = (title: string, items: AccessAccount[], isPending: boolean, empty: string) => (
    <section aria-label={title}>
      <h4 className="text-small font-semibold text-text-muted">
        {title} <span className="font-mono">{items.length}</span>
      </h4>
      {items.length ? (
        <ul role="list" className="divide-y divide-border">
          {items.map((item, i) => (
            <PersonRow
              key={`${String(item.sender_id ?? i)}`}
              item={item}
              channel={name}
              pending={isPending}
              write={write}
              locked={locked}
              onRevoke={onRevoke}
            />
          ))}
        </ul>
      ) : (
        <p className="py-1 text-small text-text-muted">{empty}</p>
      )}
    </section>
  )

  return (
    <div
      className={cn(
        'flex flex-col gap-3 rounded-md border p-3',
        pending.length ? 'border-warn/50 bg-warn/5' : 'border-border',
      )}
    >
      <div>
        <h3 className="font-medium">{t('webSetAccess.chPairingTitle')}</h3>
        <p className="text-small text-text-muted">{groups}</p>
      </div>
      {locked ? (
        <p role="status" className="flex items-center gap-1.5 text-small text-warn">
          <Lock className="size-3.5" aria-hidden data-status-icon />
          {t('webSetAccess.chPairingLocked')}
        </p>
      ) : null}
      {group(t('webSetAccess.chPendingTitle'), pending, true, t('webSetAccess.chNoPending'))}
      {group(t('webSetAccess.chPairedTitle'), paired, false, t('webSetAccess.chNoPaired'))}
      <GateReason gate={write} />
    </div>
  )
}

// ── Channel card ────────────────────────────────────────────────────────────

function ChannelCard({
  channel,
  guided,
  write,
  onConfigure,
  onRevoke,
}: {
  channel: MergedChannel
  guided: boolean | null
  write: Gate
  onConfigure: () => void
  onRevoke: (target: PairingTarget) => void
}) {
  const d = channelDisplay(channel)
  const hint = statusHint({
    status: d.status,
    isRunning: d.isRunning,
    isDead: d.isDead,
    enabled: channel.enabled !== false,
    name: d.name,
  })
  return (
    <li>
      <article
        aria-label={t('webSetAccess.chCardLabel', { name: d.name })}
        className="flex flex-col gap-3 rounded-lg border border-border bg-surface-1 p-4"
      >
        <header className="flex flex-wrap items-center gap-2">
          <ChannelIcon channel={String(channel.type || '')} className="text-text-muted" />
          <div className="min-w-0 flex-1">
            <p className="truncate font-medium">{d.name}</p>
            <p className="font-mono text-small text-text-muted">
              {channel.type || t('webSetAccess.chTypeUnknown')}
            </p>
          </div>
          <span
            className={cn(
              'rounded-full border border-current px-2 py-0.5 font-mono text-small',
              TONE_CLASS[d.tone],
            )}
          >
            {d.status}
          </span>
          {guided === false ? (
            <Button asChild size="sm" variant="ghost">
              <Link to="/settings/advanced">
                <Settings2 />
                {t('webSetAccess.chAdvanced')}
              </Link>
            </Button>
          ) : (
            <Button size="sm" variant="ghost" disabled={guided === null} onClick={onConfigure}>
              <Settings2 />
              {t('webSetAccess.chConfigure')}
            </Button>
          )}
        </header>
        <dl className="grid grid-cols-2 gap-2 text-small">
          <div>
            <dt className="text-text-muted">{t('webSetAccess.chConnectedSince')}</dt>
            <dd className="font-mono">
              {channel.connected_since ? relTime(channel.connected_since) : '—'}
            </dd>
          </div>
          <div>
            <dt className="text-text-muted">{t('webSetAccess.chRestarts')}</dt>
            <dd className="font-mono">{d.attempts}</dd>
          </div>
        </dl>
        <p className="text-small text-text-muted">{hint}</p>
        <AccessPanel channel={channel} write={write} onRevoke={onRevoke} />
        <details>
          <summary className="cursor-pointer text-small text-text-muted">
            {t('webSetAccess.chAdapterConfig')}
          </summary>
          <pre
            tabIndex={0}
            className="mt-2 max-h-64 overflow-auto rounded bg-surface-0 p-2 font-mono text-small"
          >
            {d.configJson}
          </pre>
        </details>
      </article>
    </li>
  )
}

function Stat({
  label,
  value,
  hint,
  warn,
}: {
  label: string
  value: number
  hint: string
  warn?: boolean
}) {
  return (
    <div
      className={cn(
        'flex flex-col rounded-lg border p-3',
        warn ? 'border-warn/50 bg-warn/5' : 'border-border',
      )}
    >
      <dt className="text-small text-text-muted">{label}</dt>
      <dd className="font-mono text-xl font-semibold">{value}</dd>
      <dd className="text-small text-text-muted">{hint}</dd>
    </div>
  )
}

// ── Setup dialog ────────────────────────────────────────────────────────────

type DraftValue = { value: string; checked: boolean }
type Draft = Record<string, DraftValue>

const isSecretField = (field: FieldSpec) => Boolean(field.secret || field.type === 'password')

function seedDraft(fields: FieldSpec[], initial?: Record<string, unknown>): Draft {
  return Object.fromEntries(
    fields.map((field) => {
      // Secrets are write-only: never seeded from the stored entry.
      const seed = (isSecretField(field) ? undefined : initial?.[field.name]) ?? field.default
      return [
        field.name,
        {
          value: field.type === 'bool' ? '' : String(seed ?? ''),
          checked: field.type === 'bool' ? Boolean(seed) : false,
        },
      ]
    }),
  )
}

function fieldVisible(field: FieldSpec, draft: Draft): boolean {
  return Object.entries(field.showWhen ?? {}).every(([name, expected]) =>
    typeof expected === 'boolean'
      ? draft[name]?.checked === expected
      : String(draft[name]?.value ?? '') === String(expected ?? ''),
  )
}

const inputClass =
  'h-9 w-full rounded-md border border-border-control bg-surface-0 px-3 disabled:opacity-50 touch:h-11 touch:text-base'

function SetupField({
  field,
  value,
  disabled,
  keepSecret,
  onChange,
}: {
  field: FieldSpec
  value: DraftValue
  disabled: boolean
  keepSecret: boolean
  onChange: (next: Partial<DraftValue>) => void
}) {
  const id = useId()
  const [shown, toggleShown] = useReveal()
  const label = field.label || field.name
  const help = field.description || field.help
  const describedBy = help ? `${id}-help` : undefined
  let control: React.ReactNode
  if (field.type === 'bool') {
    control = (
      <Switch
        id={id}
        aria-describedby={describedBy}
        checked={value.checked}
        disabled={disabled}
        onCheckedChange={(checked) => onChange({ checked })}
      />
    )
  } else if (field.type === 'select') {
    control = (
      <select
        id={id}
        aria-describedby={describedBy}
        className={inputClass}
        value={value.value}
        disabled={disabled}
        onChange={(e) => onChange({ value: e.target.value })}
      >
        {(field.choices ?? []).map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </select>
    )
  } else {
    const secret = isSecretField(field)
    control = (
      <div className="flex items-center gap-2">
        <input
          id={id}
          aria-describedby={describedBy}
          type={
            secret && !shown
              ? 'password'
              : field.type === 'int' || field.type === 'float'
                ? 'number'
                : 'text'
          }
          step={field.type === 'float' ? 'any' : undefined}
          autoComplete="off"
          className={inputClass}
          value={value.value}
          disabled={disabled}
          placeholder={
            field.placeholder || (secret && keepSecret ? t('webSetAccess.chSecretKeep') : '')
          }
          onChange={(e) => onChange({ value: e.target.value })}
        />
        {secret ? (
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={shown ? t('webSetAccess.hide') : t('webSetAccess.reveal')}
            onClick={toggleShown}
          >
            {shown ? <EyeOff /> : <Eye />}
          </Button>
        ) : null}
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-1" data-field={field.name}>
      <label htmlFor={id} className="font-medium">
        {label}
        {field.required ? <span aria-hidden> *</span> : null}
      </label>
      {help ? (
        <p id={`${id}-help`} className="text-small text-text-muted">
          {help}
        </p>
      ) : null}
      {control}
    </div>
  )
}

interface UpsertResult {
  restartRequired?: boolean
  warnings?: string[]
}

function ChannelSetupDialog({
  editingName,
  runtimeChannels,
  write,
  onClose,
}: {
  editingName?: string
  runtimeChannels: MergedChannel[]
  write: Gate
  onClose: () => void
}) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const snapshotQuery = useQuery<SettingsSnapshot>({
    queryKey: SETTINGS_SNAPSHOT_QUERY_KEY,
    queryFn: () => loadSettingsSnapshot(rpc),
  })
  const snapshot = snapshotQuery.data
  const specs = useMemo(() => snapshot?.catalog?.channels ?? [], [snapshot])
  const initialEntry = editingName
    ? (snapshot?.config?.channels?.channels ?? []).find(
        (entry) => String(entry.name || '') === editingName,
      )
    : undefined
  const initialType = String(
    initialEntry?.type || runtimeChannels.find((c) => c.name === editingName)?.type || '',
  )

  const [selected, setSelected] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const [baseRevision, setBaseRevision] = useState<string | undefined>()
  const [dirty, setDirty] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [confirmDiscard, setConfirmDiscard] = useState(false)

  // The adapter shown: the user's pick, else the edited entry's, else the first.
  const type =
    selected ??
    (specs.some((s) => s.type === initialType) ? initialType : editingName ? '' : specs[0]?.type)
  const spec = specs.find((s) => s.type === type)
  const isEditing = Boolean(editingName)
  const canKeepSecret = Boolean(initialEntry && String(initialEntry.type || '') === type)
  const draft =
    (type && drafts[type]) ||
    seedDraft(spec?.fields ?? [], canKeepSecret ? initialEntry : undefined)
  const visible = (spec?.fields ?? []).filter((f) => fieldVisible(f, draft))
  const snapshotRevision = typeof snapshot?.revision === 'string' ? snapshot.revision : undefined
  const writeBlocked =
    snapshot?.writeBlocked === true ||
    snapshot?.diskDiverged === true ||
    snapshot?.revision === null
  const conflicted = Boolean(
    dirty && baseRevision && snapshotRevision && baseRevision !== snapshotRevision,
  )

  const save = useMutation({
    mutationFn: async (entry: Record<string, unknown>) => {
      const expectedRevision = baseRevision ?? snapshotRevision
      await rpc.call('onboarding.channel.probe', { entry })
      return rpc.call<UpsertResult>('onboarding.channel.upsert', {
        entry,
        ...(expectedRevision ? { expectedRevision } : {}),
      })
    },
    onMutate: () => setMessage(null),
    onSuccess: async (result) => {
      setDirty(false)
      if (result?.restartRequired) useRestartNotice.getState().request()
      toast.success(t('webSetAccess.chSaved'))
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: CHANNELS_KEY }),
        queryClient.invalidateQueries({ queryKey: SETTINGS_SNAPSHOT_QUERY_KEY }),
      ])
      onClose()
    },
    onError: (e) => {
      const text = errorMessage(e)
      if (text.includes('config revision mismatch')) {
        setMessage(t('webSetAccess.chConflict'))
        void snapshotQuery.refetch()
      } else setMessage(t('webSetAccess.chSaveFailed', { message: text }))
    },
  })
  const saving = save.isPending

  const update = (name: string, next: Partial<DraftValue>) => {
    if (!type) return
    if (!dirty) {
      setDirty(true)
      setBaseRevision(snapshotRevision)
    }
    setMessage(null)
    setDrafts((current) => ({
      ...current,
      [type]: { ...draft, [name]: { ...(draft[name] ?? { value: '', checked: false }), ...next } },
    }))
  }

  const submit = () => {
    if (!spec || !type) return
    const fields: ScopedField[] = visible.map((field) => ({
      name: field.name,
      value: draft[field.name]?.value ?? '',
      checked: draft[field.name]?.checked ?? false,
      type: field.type === 'bool' ? 'checkbox' : isSecretField(field) ? 'password' : 'text',
      secret: isSecretField(field),
      required: Boolean(field.required),
      hidden: false,
      label: field.label,
    }))
    const missing = validateScopedRequiredFields(fields, canKeepSecret)
    if (missing) {
      setMessage(t('webSetAccess.chRequired', { field: missing }))
      return
    }
    // An edit keeps the entry's other keys; secrets are only sent when typed.
    const secretNames = new Set((spec.fields ?? []).filter(isSecretField).map((f) => f.name))
    const preserved =
      isEditing && initialEntry
        ? Object.fromEntries(Object.entries(initialEntry).filter(([k]) => !secretNames.has(k)))
        : {}
    const entry: Record<string, unknown> = {
      ...preserved,
      type,
      ...readScopedFields(fields, 'channel'),
    }
    const name = String(entry.name || '').trim()
    if (runtimeChannels.some((c) => String(c.name || '') === name && c.name !== editingName)) {
      setMessage(t('webSetAccess.chDuplicate', { name }))
      return
    }
    save.mutate(entry)
  }

  const requestClose = () => {
    if (saving) return
    if (dirty) setConfirmDiscard(true)
    else onClose()
  }

  const primary = visible.filter((f) => !f.advanced)
  const advanced = visible.filter((f) => f.advanced)
  const fieldList = (list: FieldSpec[]) =>
    list.map((field) => (
      <SetupField
        key={field.name}
        field={field}
        value={draft[field.name] ?? { value: '', checked: false }}
        disabled={saving || (isEditing && field.name === 'name')}
        keepSecret={canKeepSecret}
        onChange={(next) => update(field.name, next)}
      />
    ))

  let body: React.ReactNode
  if (snapshotQuery.isPending) {
    body = (
      <LoadingRegion>
        <Skeleton className="h-32" />
      </LoadingRegion>
    )
  } else if (snapshotQuery.isError) {
    body = (
      <ErrorState
        title={t('webSetAccess.chSetupLoadFailed')}
        message={errorMessage(snapshotQuery.error)}
        onRetry={() => void snapshotQuery.refetch()}
      />
    )
  } else if (specs.length === 0) {
    body = <EmptyState title={t('webSetAccess.chNoAdapters')} />
  } else if (isEditing && initialType && !spec) {
    body = (
      <EmptyState
        title={t('webSetAccess.chNeedsAdvanced', { type: initialType })}
        action={
          <Button asChild variant="outline">
            <Link to="/settings/advanced">{t('webSetAccess.chOpenAdvanced')}</Link>
          </Button>
        }
      />
    )
  } else {
    body = (
      <>
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-2 font-medium">{t('webSetAccess.chAdapter')}</legend>
          <div
            role="radiogroup"
            aria-label={t('webSetAccess.chAdapter')}
            className="grid gap-2 sm:grid-cols-2"
          >
            {specs.map((candidate) => (
              <button
                key={candidate.type}
                type="button"
                role="radio"
                aria-checked={type === candidate.type}
                disabled={saving || (isEditing && type !== candidate.type)}
                onClick={() => {
                  if (isEditing || candidate.type === type) return
                  setSelected(candidate.type)
                  if (!dirty) {
                    setDirty(true)
                    setBaseRevision(snapshotRevision)
                  }
                }}
                className={cn(
                  'target flex items-center gap-2 rounded-md border px-3 py-2 text-left disabled:opacity-50',
                  type === candidate.type
                    ? 'border-accent bg-accent-soft'
                    : 'border-border hover:bg-surface-3',
                )}
              >
                <ChannelIcon channel={candidate.type} className="text-text-muted" />
                <span className="min-w-0">
                  <span className="block font-medium">{candidate.label || candidate.type}</span>
                  <span className="block text-small text-text-muted">
                    {candidate.transport || candidate.type}
                  </span>
                </span>
              </button>
            ))}
          </div>
          {isEditing ? (
            <p className="text-small text-text-muted">{t('webSetAccess.chTypeLocked')}</p>
          ) : null}
        </fieldset>
        {spec ? (
          <div className="flex flex-col gap-3">
            {spec.description || spec.help ? (
              <p className="text-text-muted">{spec.description || spec.help}</p>
            ) : null}
            {spec.whatYouNeed?.length ? (
              <div className="rounded-md border border-border p-3">
                <p className="font-medium">{t('webSetAccess.chBeforeYouStart')}</p>
                <ul className="list-disc pl-5 text-small">
                  {spec.whatYouNeed.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {spec.docsHint ? (
              <a
                href={spec.docsHint}
                target="_blank"
                rel="noreferrer"
                className="text-small text-accent underline-offset-2 hover:underline"
              >
                {t('webSetAccess.chSetupGuide')}
              </a>
            ) : null}
            {fieldList(primary)}
            {advanced.length ? (
              <details className="flex flex-col gap-3">
                <summary className="cursor-pointer text-small text-text-muted">
                  {t('webSetAccess.chAdvancedOptions')}
                </summary>
                <div className="mt-3 flex flex-col gap-3">{fieldList(advanced)}</div>
              </details>
            ) : null}
          </div>
        ) : null}
      </>
    )
  }

  const blockedReason = writeBlocked
    ? t('webSetAccess.chWriteBlocked')
    : conflicted
      ? t('webSetAccess.chConflict')
      : null

  return (
    <>
      <Dialog open onOpenChange={(open) => (open ? undefined : requestClose())}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {isEditing
                ? t('webSetAccess.chSetupEditTitle', { name: String(editingName) })
                : t('webSetAccess.chSetupAddTitle')}
            </DialogTitle>
            <DialogDescription>{t('webSetAccess.chSetupBody')}</DialogDescription>
          </DialogHeader>
          {body}
          {blockedReason ? (
            <div role="alert" className="flex flex-wrap items-center gap-2 text-small text-danger">
              <span className="flex-1">{blockedReason}</span>
              {conflicted ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setBaseRevision(snapshotRevision)
                    setMessage(null)
                  }}
                >
                  {t('webSetAccess.chUseLatest')}
                </Button>
              ) : null}
            </div>
          ) : message ? (
            <p role="alert" className="text-small text-danger">
              {message}
            </p>
          ) : (
            <p className="flex items-center gap-1.5 text-small text-text-muted">
              <Lock className="size-3.5" aria-hidden />
              {t('webSetAccess.chWriteOnly')}
            </p>
          )}
          <DialogFooter className="items-center">
            <GateReason gate={write} className="mr-auto" />
            <Button variant="outline" disabled={saving} onClick={requestClose}>
              {t('webCommon.cancel')}
            </Button>
            <Button
              variant="primary"
              disabled={saving || !spec || writeBlocked || conflicted || write.disabled}
              onClick={submit}
            >
              {saving ? <Loader2 className="animate-spin" /> : null}
              {saving
                ? t('webSetAccess.chValidating')
                : isEditing
                  ? t('webSetAccess.chSaveUpdate')
                  : t('webSetAccess.chSaveAdd')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <AlertDialog open={confirmDiscard} onOpenChange={setConfirmDiscard}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('webSetAccess.discardTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('webSetAccess.discardBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webSetAccess.discardKeep')}</AlertDialogCancel>
            <AlertDialogAction destructive onClick={onClose}>
              {t('webSetAccess.discardConfirm')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

// ── Section ─────────────────────────────────────────────────────────────────

interface ChannelsStatus {
  channels?: RawChannel[]
}
interface AccessList {
  channels?: ChannelAccess[]
}

export function ChannelsSection() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const write = useWriteGate()
  const [params, setParams] = useSearchParams()
  const [revoking, setRevoking] = useState<PairingTarget | null>(null)
  const setupOpen = params.get('setup') === '1'
  const editingName = params.get('channel') || undefined

  useRegisterFields('channels', [
    { id: 'channels.add', label: t('webSetAccess.chAdd') },
    { id: 'channels.pairing', label: t('webSetAccess.chPairingTitle') },
  ])

  const channelsQuery = useQuery<MergedChannel[]>({
    queryKey: CHANNELS_KEY,
    queryFn: async () => {
      await rpc.waitForConnection()
      // Pairing is optional: channel health still shows when it fails.
      const [status, pairing] = await Promise.all([
        rpc.call<ChannelsStatus>('channels.status', {}),
        rpc.call<AccessList>('channels.pairing.list', {}).catch(() => ({}) as AccessList),
      ])
      return sortChannels(mergeChannels(status?.channels, pairing?.channels))
    },
    refetchInterval: POLL_MS,
    refetchOnWindowFocus: false,
  })
  const snapshotQuery = useQuery<SettingsSnapshot>({
    queryKey: SETTINGS_SNAPSHOT_QUERY_KEY,
    queryFn: () => loadSettingsSnapshot(rpc),
  })

  useEffect(
    () =>
      rpc.on('channel.status', () => {
        void queryClient.invalidateQueries({ queryKey: CHANNELS_KEY })
      }),
    [rpc, queryClient],
  )

  const revoke = useMutation({
    mutationFn: (target: PairingTarget) =>
      rpc.call('channels.pairing.revoke', { channel: target.channel, senderId: target.senderId }),
    onSuccess: (_data, target) => {
      toast.success(t('webSetAccess.chRevokedToast', { name: target.label }))
      void queryClient.invalidateQueries({ queryKey: CHANNELS_KEY })
    },
    onError: (e) => toast.error(t('webSetAccess.chPairingFailed', { message: errorMessage(e) })),
  })

  const openSetup = (channel?: string) => {
    const next = new URLSearchParams(params)
    next.set('setup', '1')
    if (channel) next.set('channel', channel)
    else next.delete('channel')
    setParams(next)
  }
  const closeSetup = () => {
    const next = new URLSearchParams(params)
    next.delete('setup')
    next.delete('channel')
    setParams(next, { replace: true })
  }

  const channels = channelsQuery.data ?? []
  const stats = channelStats(channels)
  const guidedTypes = new Set((snapshotQuery.data?.catalog?.channels ?? []).map((s) => s.type))

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center gap-2" data-field-id="channels.add">
        <Button variant="primary" disabled={write.disabled} onClick={() => openSetup()}>
          <Plus />
          {t('webSetAccess.chAdd')}
        </Button>
        <Button
          variant="outline"
          disabled={channelsQuery.isFetching}
          onClick={() => void queryClient.invalidateQueries({ queryKey: CHANNELS_KEY })}
        >
          <RefreshCw className={channelsQuery.isFetching ? 'animate-spin' : undefined} />
          {t('webSetAccess.refresh')}
        </Button>
        <span className="text-small text-text-muted">{t('webSetAccess.chCadence')}</span>
        <GateReason gate={write} className="w-full" />
      </div>

      {channelsQuery.isPending ? (
        <LoadingRegion>
          <Skeleton className="h-40" />
        </LoadingRegion>
      ) : channelsQuery.isError && channels.length === 0 ? (
        <ErrorState
          title={t('webSetAccess.chLoadFailed')}
          message={errorMessage(channelsQuery.error)}
          onRetry={() => void channelsQuery.refetch()}
        />
      ) : (
        <>
          <dl
            aria-label={t('webSetAccess.chStats')}
            className="grid grid-cols-2 gap-2 sm:grid-cols-5"
          >
            <Stat
              label={t('webSetAccess.chStatTotal')}
              value={stats.total}
              hint={tPlural('webSetAccess.chStatTypes', stats.typeCount)}
            />
            <Stat
              label={t('webSetAccess.chStatConnected')}
              value={stats.connected}
              hint={t('webSetAccess.chStatConnectedHint')}
            />
            <Stat
              label={t('webSetAccess.chStatAttention')}
              value={stats.attention}
              warn={stats.attention > 0}
              hint={t('webSetAccess.chStatInactive', { count: stats.inactive })}
            />
            <Stat
              label={t('webSetAccess.chStatRestarts')}
              value={stats.restarts}
              hint={t('webSetAccess.chStatRestartsHint')}
            />
            <Stat
              label={t('webSetAccess.chStatPairing')}
              value={stats.pendingAccess}
              warn={stats.pendingAccess > 0}
              hint={
                stats.pendingAccess
                  ? t('webSetAccess.chStatPairingWaiting')
                  : t('webSetAccess.chStatPairingIdle')
              }
            />
          </dl>
          {channelsQuery.isError ? (
            <ErrorState
              title={t('webSetAccess.chLoadFailed')}
              message={errorMessage(channelsQuery.error)}
              onRetry={() => void channelsQuery.refetch()}
            />
          ) : null}
          {channels.length === 0 ? (
            <EmptyState
              title={t('webSetAccess.chEmptyTitle')}
              description={t('webSetAccess.chEmptyBody')}
              action={
                <Button variant="primary" disabled={write.disabled} onClick={() => openSetup()}>
                  <Plus />
                  {t('webSetAccess.chAdd')}
                </Button>
              }
            />
          ) : (
            <ul role="list" className="flex flex-col gap-3" data-field-id="channels.pairing">
              {channels.map((channel, i) => (
                <ChannelCard
                  key={String(channel.name || channel.id || i)}
                  channel={channel}
                  guided={
                    snapshotQuery.isPending ? null : guidedTypes.has(String(channel.type || ''))
                  }
                  write={write}
                  onConfigure={() => openSetup(String(channel.name || ''))}
                  onRevoke={setRevoking}
                />
              ))}
            </ul>
          )}
        </>
      )}

      {setupOpen ? (
        <ChannelSetupDialog
          key={editingName ?? 'new'}
          editingName={editingName}
          runtimeChannels={channels}
          write={write}
          onClose={closeSetup}
        />
      ) : null}

      <AlertDialog
        open={revoking !== null}
        onOpenChange={(open) => (open ? null : setRevoking(null))}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('webSetAccess.chRevokeTitle', { name: revoking?.label ?? '' })}
            </AlertDialogTitle>
            <AlertDialogDescription>{t('webSetAccess.chRevokeBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webCommon.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              destructive
              disabled={write.disabled}
              onClick={() => {
                if (revoking) revoke.mutate(revoking)
                setRevoking(null)
              }}
            >
              {t('webSetAccess.chRevoke')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
