import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Check,
  CircleAlert,
  ExternalLink,
  ListChecks,
  Loader2,
  Lock,
  MessagesSquare,
  Plus,
  Radio,
  RefreshCw,
  Settings2,
  SlidersHorizontal,
  UserCheck,
  X,
} from 'lucide-react'
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
import { Badge, StatusDot } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Card, StatCard } from '@/web/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { Input, Select } from '@/web/components/ui/input'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { Switch } from '@/web/components/ui/switch'
import { ChannelIcon } from '@/web/shell/channel-icon'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useRestartNotice } from '../config-editor'
import { SecretInput, useRegisterFields, useReveal } from '../fields'
import { Disclosure, Notice } from '../ui'

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

const STATUS_TONE = { ok: 'ok', danger: 'danger', off: 'neutral' } as const

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
    <li className="flex flex-col gap-1.5 px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2.5">
        <span
          aria-hidden
          className={cn(
            'grid size-8 shrink-0 place-items-center rounded-full text-small font-semibold uppercase',
            pending ? 'bg-warn/15 text-warn' : 'bg-surface-3 text-text-muted',
          )}
        >
          {label.replace(/^@/, '').slice(0, 1) || '?'}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{label}</p>
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
              {busy && resolve.variables ? <Loader2 className="animate-spin" /> : <Check />}
              {t('webSetAccess.chApprove')}
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="text-danger hover:bg-danger/10"
              disabled={write.disabled || busy}
              onClick={() => resolve.mutate(false)}
            >
              <X />
              {t('webSetAccess.chDeny')}
            </Button>
          </>
        ) : (
          <Button
            size="sm"
            variant="ghost"
            className="text-text-muted hover:text-danger"
            disabled={write.disabled}
            onClick={() => onRevoke({ channel, senderId, label })}
          >
            {t('webSetAccess.chRevoke')}
          </Button>
        )}
      </div>
      {error ? (
        <p role="alert" className="ml-10 flex items-center gap-1.5 text-small text-danger">
          <CircleAlert className="size-3.5 shrink-0" aria-hidden data-status-icon />
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
    <section aria-label={title} className="flex flex-col gap-1.5">
      <h4 className="flex items-center gap-2 text-label text-text-muted">
        {title}{' '}
        <span
          className={cn(
            'rounded-full px-1.5 font-mono tabular-nums',
            isPending && items.length ? 'bg-warn/15 text-warn' : 'bg-surface-3',
          )}
        >
          {items.length}
        </span>
      </h4>
      {items.length ? (
        <ul role="list" className="set-list shadow-none">
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
        <p className="rounded-control border border-dashed border-border px-3 py-2 text-small text-text-muted">
          {empty}
        </p>
      )}
    </section>
  )

  return (
    <div
      className={cn(
        'flex flex-col gap-3 rounded-control border p-3',
        pending.length ? 'border-warn/30 bg-warn/5' : 'border-border bg-surface-2',
      )}
    >
      <div className="flex items-start gap-2">
        <UserCheck className="mt-0.5 size-4 shrink-0 text-text-muted" aria-hidden />
        <div className="min-w-0">
          <h3 className="font-medium">{t('webSetAccess.chPairingTitle')}</h3>
          <p className="text-small text-text-muted">{groups}</p>
        </div>
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
      <Card asChild className="flex flex-col gap-3.5">
        <article aria-label={t('webSetAccess.chCardLabel', { name: d.name })}>
          <header className="flex flex-wrap items-center gap-3">
            <span
              aria-hidden
              className="grid size-9 shrink-0 place-items-center rounded-control bg-surface-2 text-text-muted shadow-highlight"
            >
              <ChannelIcon channel={String(channel.type || '')} />
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate font-medium">{d.name}</p>
              <p className="font-mono text-small text-text-muted">
                {channel.type || t('webSetAccess.chTypeUnknown')}
              </p>
            </div>
            <Badge tone={STATUS_TONE[d.tone]} dot live={d.tone === 'ok'} className="font-mono">
              {d.status}
            </Badge>
            {guided === false ? (
              <Button asChild size="sm" variant="outline">
                <Link to="/settings/advanced">
                  <Settings2 />
                  {t('webSetAccess.chAdvanced')}
                </Link>
              </Button>
            ) : (
              <Button size="sm" variant="outline" disabled={guided === null} onClick={onConfigure}>
                <Settings2 />
                {t('webSetAccess.chConfigure')}
              </Button>
            )}
          </header>
          <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-control border border-border bg-border text-small">
            <div className="bg-surface-2 px-3 py-2">
              <dt className="text-label text-text-muted">{t('webSetAccess.chConnectedSince')}</dt>
              <dd className="font-mono tabular-nums">
                {channel.connected_since ? relTime(channel.connected_since) : '—'}
              </dd>
            </div>
            <div className="bg-surface-2 px-3 py-2">
              <dt className="text-label text-text-muted">{t('webSetAccess.chRestarts')}</dt>
              <dd className="font-mono tabular-nums">{d.attempts}</dd>
            </div>
          </dl>
          <p className="text-small text-text-muted">{hint}</p>
          <AccessPanel channel={channel} write={write} onRevoke={onRevoke} />
          <Disclosure summary={t('webSetAccess.chAdapterConfig')} className="text-small">
            <pre
              tabIndex={0}
              className="max-h-64 overflow-auto rounded-control bg-surface-inset p-3 font-mono text-small shadow-inset"
            >
              {d.configJson}
            </pre>
          </Disclosure>
        </article>
      </Card>
    </li>
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
      <Select
        id={id}
        aria-describedby={describedBy}
        value={value.value}
        disabled={disabled}
        onChange={(e) => onChange({ value: e.target.value })}
      >
        {(field.choices ?? []).map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </Select>
    )
  } else {
    const secret = isSecretField(field)
    const common = {
      id,
      'aria-describedby': describedBy,
      value: value.value,
      disabled,
      className: 'disabled:opacity-50',
      placeholder:
        field.placeholder || (secret && keepSecret ? t('webSetAccess.chSecretKeep') : ''),
      onChange: (e: React.ChangeEvent<HTMLInputElement>) => onChange({ value: e.target.value }),
    }
    control = secret ? (
      <SecretInput {...common} shown={shown} onToggle={toggleShown} />
    ) : (
      <Input
        {...common}
        type={field.type === 'int' || field.type === 'float' ? 'number' : 'text'}
        step={field.type === 'float' ? 'any' : undefined}
        autoComplete="off"
      />
    )
  }
  return (
    <div
      className={cn(
        'flex gap-1.5',
        field.type === 'bool' ? 'flex-row-reverse items-start justify-end gap-3' : 'flex-col',
      )}
      data-field={field.name}
    >
      <div className="min-w-0">
        <label htmlFor={id} className="font-medium">
          {label}
          {field.required ? (
            <span aria-hidden className="text-accent">
              {' '}
              *
            </span>
          ) : null}
        </label>
        {help ? (
          <p id={`${id}-help`} className="text-small text-pretty text-text-muted">
            {help}
          </p>
        ) : null}
      </div>
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
      <LoadingRegion className="flex flex-col gap-3">
        <div className="grid gap-2 sm:grid-cols-2">
          <Skeleton className="h-14 rounded-control" />
          <Skeleton className="h-14 rounded-control" />
        </div>
        <Skeleton className="h-9 rounded-control" />
        <Skeleton className="h-9 rounded-control" />
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
    body = <EmptyState size="sm" icon={<Radio />} title={t('webSetAccess.chNoAdapters')} />
  } else if (isEditing && initialType && !spec) {
    body = (
      <EmptyState
        size="sm"
        icon={<SlidersHorizontal />}
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
          <legend className="mb-2 text-label text-text-muted">{t('webSetAccess.chAdapter')}</legend>
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
                  'press target flex items-center gap-3 rounded-control border px-3 py-2.5 text-left disabled:opacity-50',
                  type === candidate.type
                    ? 'border-accent/60 bg-accent-tint shadow-[0_0_0_1px_var(--accent-tint)] forced-colors:border-[Highlight]'
                    : 'border-border bg-surface-2 hover:border-border-strong hover:bg-surface-3',
                )}
              >
                <span
                  aria-hidden
                  className={cn(
                    'grid size-8 shrink-0 place-items-center rounded-control',
                    type === candidate.type
                      ? 'bg-accent-tint text-accent'
                      : 'bg-surface-3 text-text-muted',
                  )}
                >
                  <ChannelIcon channel={candidate.type} />
                </span>
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
          <div key={type} className="anim-fade-in flex flex-col gap-3.5">
            {spec.description || spec.help ? (
              <p className="text-text-muted">{spec.description || spec.help}</p>
            ) : null}
            {spec.whatYouNeed?.length ? (
              <Notice tone="info" icon={<ListChecks data-status-icon />}>
                <p className="font-medium">{t('webSetAccess.chBeforeYouStart')}</p>
                <ul className="mt-1 list-disc pl-5 text-small text-text-muted">
                  {spec.whatYouNeed.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </Notice>
            ) : null}
            {spec.docsHint ? (
              <a
                href={spec.docsHint}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 self-start text-small text-accent underline-offset-2 hover:underline"
              >
                {t('webSetAccess.chSetupGuide')}
                <ExternalLink className="size-3.5" aria-hidden />
              </a>
            ) : null}
            {fieldList(primary)}
            {advanced.length ? (
              <Disclosure
                summary={t('webSetAccess.chAdvancedOptions')}
                className="border-t border-border pt-2"
                bodyClassName="flex flex-col gap-3.5"
              >
                {fieldList(advanced)}
              </Disclosure>
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
            <Notice
              role="alert"
              tone="danger"
              className="text-small"
              actions={
                conflicted ? (
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
                ) : null
              }
            >
              {blockedReason}
            </Notice>
          ) : message ? (
            <Notice role="alert" tone="danger" className="anim-fade-in text-small">
              {message}
            </Notice>
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
    <div className="flex flex-col gap-5">
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
        <span className="inline-flex items-center gap-1.5 text-small text-text-muted">
          <StatusDot tone="ok" live className="size-1.5" />
          {t('webSetAccess.chCadence')}
        </span>
        <GateReason gate={write} className="w-full" />
      </div>

      {channelsQuery.isPending ? (
        <LoadingRegion className="flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-2.5 @lg:grid-cols-3 @2xl:grid-cols-5">
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-24 rounded-card" />
            ))}
          </div>
          <Skeleton className="h-44 rounded-card" />
        </LoadingRegion>
      ) : channelsQuery.isError && channels.length === 0 ? (
        <ErrorState
          title={t('webSetAccess.chLoadFailed')}
          message={errorMessage(channelsQuery.error)}
          onRetry={() => void channelsQuery.refetch()}
        />
      ) : (
        <>
          <div
            role="group"
            aria-label={t('webSetAccess.chStats')}
            className="anim-stagger grid grid-cols-2 gap-2.5 @lg:grid-cols-3 @2xl:grid-cols-5"
          >
            <StatCard
              headingAs="p"
              label={t('webSetAccess.chStatTotal')}
              value={stats.total}
              hint={tPlural('webSetAccess.chStatTypes', stats.typeCount)}
            />
            <StatCard
              headingAs="p"
              label={t('webSetAccess.chStatConnected')}
              value={stats.connected}
              tone={stats.connected > 0 ? 'ok' : 'default'}
              hint={t('webSetAccess.chStatConnectedHint')}
            />
            <StatCard
              headingAs="p"
              label={t('webSetAccess.chStatAttention')}
              value={stats.attention}
              tone={stats.attention > 0 ? 'warn' : 'default'}
              hint={t('webSetAccess.chStatInactive', { count: stats.inactive })}
            />
            <StatCard
              headingAs="p"
              label={t('webSetAccess.chStatRestarts')}
              value={stats.restarts}
              hint={t('webSetAccess.chStatRestartsHint')}
            />
            <StatCard
              headingAs="p"
              label={t('webSetAccess.chStatPairing')}
              value={stats.pendingAccess}
              tone={stats.pendingAccess > 0 ? 'warn' : 'default'}
              hint={
                stats.pendingAccess
                  ? t('webSetAccess.chStatPairingWaiting')
                  : t('webSetAccess.chStatPairingIdle')
              }
            />
          </div>
          {channelsQuery.isError ? (
            <ErrorState
              title={t('webSetAccess.chLoadFailed')}
              message={errorMessage(channelsQuery.error)}
              onRetry={() => void channelsQuery.refetch()}
            />
          ) : null}
          {channels.length === 0 ? (
            <EmptyState
              className="set-card border-dashed shadow-none"
              icon={<MessagesSquare />}
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
            <ul
              role="list"
              className="anim-stagger flex flex-col gap-3"
              data-field-id="channels.pairing"
            >
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
