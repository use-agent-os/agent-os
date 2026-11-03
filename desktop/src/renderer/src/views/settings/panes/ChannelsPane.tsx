import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LoaderCircle, Plus, RefreshCw } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { ModalShell } from '@/components/ModalShell'
import { useConnection } from '@/stores/connection'
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
import type { ChannelSpec } from '@/views/setup/logic'
import { Button } from '~/components/ui/button'
import { Switch } from '~/components/ui/switch'
import { t, type MessageKey } from '~/i18n'
import { desktopApi, isDesktop } from '~/lib/desktop-api'
import { useGateway } from '~/stores/gateway'
import { AdapterMark } from '../AdapterMark'
import {
  canRestartChannel,
  errorText,
  hasGuidedSetup,
  isRevisionConflict,
  statusTone,
} from '../channels-logic'
import { Card, Head, Notice, Pill, Row, Value } from '../parts'
import { PendingRestartNotice } from '../PendingRestartNotice'
import { useConfigSnapshot, withRevision } from '../use-snapshot'
import { ChannelSetupSheet } from './ChannelSetupSheet'

export const CHANNELS_KEY = ['settings', 'channels'] as const

/** The console's cadence: runtime status every 5 s while the section is open. */
const POLL_MS = 5000

interface ChannelsStatus {
  channels?: RawChannel[]
}
interface AccessList {
  channels?: ChannelAccess[]
}
interface WriteResult {
  restartRequired?: boolean
  warnings?: string[]
}

function fill(key: MessageKey, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce(
    (text, [name, value]) => text.replace(`{${name}}`, String(value)),
    t(key),
  )
}

/**
 * Settings › Channels: the console's Channels screen in the app's
 * vocabulary. The same runtime reads (`channels.status`, the Telegram
 * pairing list, the `channel.status` push) and the same guided writes
 * (`onboarding.channel.probe` + `upsert`, `enable`/`disable`/`remove`)
 * carrying the config snapshot's revision, so a stale form never overwrites
 * a newer file. A managed gateway is restarted for the change to apply, as
 * Providers does; an external one is told to restart.
 */
export function ChannelsPane() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const connected = useConnection((s) => s.state === 'connected')
  const { query: snapshotQuery, snapshot, reload: reloadSnapshot } = useConfigSnapshot()
  const restartGateway = useGateway((s) => s.restart)
  const managedGateway = useGateway((s) => s.status.pid !== null)
  const [sheet, setSheet] = useState<{ editingName?: string; nonce: number } | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [removing, setRemoving] = useState<string | null>(null)
  const [busyName, setBusyName] = useState<string | null>(null)

  const list = useQuery<MergedChannel[]>({
    queryKey: CHANNELS_KEY,
    enabled: connected,
    refetchInterval: POLL_MS,
    refetchOnWindowFocus: false,
    queryFn: async () => {
      // Channel health stays readable when the pairing endpoint fails.
      const [status, pairing] = await Promise.all([
        rpc.call<ChannelsStatus>('channels.status', {}),
        rpc
          .call<AccessList>('channels.pairing.list', {})
          .catch(() => ({ channels: [] }) as AccessList),
      ])
      return sortChannels(mergeChannels(status.channels, pairing.channels))
    },
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: CHANNELS_KEY })

  useEffect(() => {
    const unsub = rpc.on('channel.status', () => void invalidate())
    return () => unsub()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rpc, queryClient])

  /** After a config write: warnings, the restart it needs, fresh reads. */
  async function settle(res: WriteResult | undefined, saved: string) {
    for (const w of res?.warnings ?? []) toast.warning(w)
    if (res?.restartRequired && managedGateway) {
      toast.success(t('settings.channels.toast.savedRestarting'), { id: 'stg-channels' })
      await restartGateway()
    } else {
      toast.success(res?.restartRequired ? t('settings.restartRequired') : saved, {
        id: 'stg-channels',
      })
    }
    await Promise.all([reloadSnapshot(), invalidate()])
  }

  const save = useMutation({
    mutationFn: async (entry: Record<string, unknown>) => {
      await rpc.call('onboarding.channel.probe', { entry })
      return rpc.call<WriteResult>('onboarding.channel.upsert', withRevision(snapshot, { entry }))
    },
    onMutate: () => {
      setSaveError(null)
      setConflict(false)
    },
    onSuccess: async (res) => {
      setSheet(null)
      await settle(res, t('settings.saved'))
    },
    onError: async (err) => {
      if (isRevisionConflict(err)) {
        setConflict(true)
        toast.error(t('settings.channels.toast.conflict'), { id: 'stg-channels-err' })
        await reloadSnapshot()
      } else {
        setSaveError(`${t('settings.saveFailed')}: ${errorText(err)}`)
      }
    },
  })

  const toggle = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      rpc.call<WriteResult>(
        enabled ? 'onboarding.channel.enable' : 'onboarding.channel.disable',
        withRevision(snapshot, { name }),
      ),
    onMutate: ({ name }) => setBusyName(name),
    onSuccess: (res, { name, enabled }) =>
      settle(
        res,
        fill(enabled ? 'settings.channels.toast.enabled' : 'settings.channels.toast.disabled', {
          name,
        }),
      ),
    onError: (err) =>
      toast.error(`${t('settings.saveFailed')}: ${errorText(err)}`, { id: 'stg-channels-err' }),
    onSettled: () => setBusyName(null),
  })

  const remove = useMutation({
    mutationFn: (name: string) =>
      rpc.call<WriteResult>('onboarding.channel.remove', withRevision(snapshot, { name })),
    onMutate: (name) => setBusyName(name),
    onSuccess: (res, name) => settle(res, fill('settings.channels.toast.removed', { name })),
    onError: (err) =>
      toast.error(`${t('settings.saveFailed')}: ${errorText(err)}`, { id: 'stg-channels-err' }),
    onSettled: () => setBusyName(null),
  })

  const restartChannel = useMutation({
    mutationFn: (name: string) => rpc.call('channels.restart', { channel: name }),
    onMutate: (name) => setBusyName(name),
    onSuccess: async (_res, name) => {
      toast.success(fill('settings.channels.toast.restarted', { name }), { id: 'stg-channels' })
      await invalidate()
    },
    onError: (err) => toast.error(errorText(err), { id: 'stg-channels-err' }),
    onSettled: () => setBusyName(null),
  })

  const resolve = useMutation({
    mutationFn: (vars: { channel: string; senderId: string; approved: boolean }) =>
      rpc.call(vars.approved ? 'channels.pairing.approve' : 'channels.pairing.deny', {
        channel: vars.channel,
        senderId: vars.senderId,
      }),
    onSuccess: async (_res, vars) => {
      toast.success(
        vars.approved ? t('settings.channels.toast.paired') : t('settings.channels.toast.denied'),
        { id: 'stg-channels' },
      )
      await invalidate()
    },
    onError: (err) => toast.error(errorText(err), { id: 'stg-channels-err' }),
  })

  const revoke = useMutation({
    mutationFn: (vars: { channel: string; senderId: string }) =>
      rpc.call('channels.pairing.revoke', vars),
    onSuccess: async () => {
      toast.success(t('settings.channels.toast.disconnected'), { id: 'stg-channels' })
      await invalidate()
    },
    onError: (err) => toast.error(errorText(err), { id: 'stg-channels-err' }),
  })

  const channels = list.data ?? []
  const stats = channelStats(channels)
  const specs: readonly ChannelSpec[] = snapshot?.catalog?.channels ?? []
  const ready = list.isSuccess
  const blocked = Boolean(snapshot?.writeBlocked)
  const pairingBusy = resolve.isPending || revoke.isPending
  const configPath = snapshot?.configPath ?? null

  function openSheet(editingName?: string) {
    setSaveError(null)
    setConflict(false)
    setSheet((s) => ({ editingName, nonce: (s?.nonce ?? 0) + 1 }))
  }

  const head = (
    <Head title={t('settings.section.channels')} blurb={t('settings.section.channels.blurb')} />
  )

  if (!connected) {
    return (
      <>
        {head}
        <Notice tone="info">{t('settings.offline')}</Notice>
      </>
    )
  }

  return (
    <>
      {head}

      {snapshot?.writeBlocked ? (
        <Notice tone="danger">{t('settings.writeBlocked')}</Notice>
      ) : snapshot?.pendingRestart ? (
        <PendingRestartNotice />
      ) : null}

      {list.isError ? (
        <Notice
          tone="danger"
          action={
            <Button onClick={() => void list.refetch()}>{t('settings.channels.retry')}</Button>
          }
        >
          {`${t('settings.channels.loadError')}: ${errorText(list.error)}`}
        </Notice>
      ) : null}

      <Card
        title={t('settings.channels.overview')}
        blurb={t('settings.channels.overview.blurb')}
        action={
          <div className="env-actions">
            <Button disabled={list.isFetching} onClick={() => void invalidate()}>
              <RefreshCw
                className={list.isFetching ? 'size-3.5 stg-spin' : 'size-3.5'}
                strokeWidth={2}
                aria-hidden
              />
              {t('settings.channels.refresh')}
            </Button>
            <Button variant="primary" disabled={blocked} onClick={() => openSheet()}>
              <Plus className="size-3.5" strokeWidth={2} aria-hidden />
              {t('settings.channels.add')}
            </Button>
          </div>
        }
      >
        <Row label={t('settings.channels.stat.total')}>
          <Value>{ready ? String(stats.total) : '—'}</Value>
        </Row>
        <Row label={t('settings.channels.stat.connected')}>
          <Value tone={ready && stats.connected > 0 ? 'ok' : undefined}>
            {ready ? String(stats.connected) : '—'}
          </Value>
        </Row>
        <Row
          label={t('settings.channels.stat.attention')}
          help={t('settings.channels.stat.attention.help')}
        >
          <Value tone={ready && stats.attention > 0 ? 'danger' : undefined}>
            {ready ? String(stats.attention) : '—'}
          </Value>
        </Row>
        <Row
          label={t('settings.channels.stat.pairing')}
          help={t('settings.channels.stat.pairing.help')}
        >
          <Value tone={ready && stats.pendingAccess > 0 ? 'warn' : undefined}>
            {ready ? String(stats.pendingAccess) : '—'}
          </Value>
        </Row>
      </Card>

      {!ready && !list.isError ? (
        <div className="flex items-center gap-2 text-muted-foreground" role="status">
          <LoaderCircle className="stg-spin size-3.5" strokeWidth={1.75} aria-hidden />
          {t('settings.channels.loading')}
        </div>
      ) : ready && channels.length === 0 ? (
        <Card
          title={t('settings.channels.empty.title')}
          blurb={t('settings.channels.empty.body')}
          action={
            <Button variant="primary" disabled={blocked} onClick={() => openSheet()}>
              <Plus className="size-3.5" strokeWidth={2} aria-hidden />
              {t('settings.channels.add')}
            </Button>
          }
        >
          <div className="stg-card__body" />
        </Card>
      ) : (
        channels.map((channel) => {
          const name = String(channel.name || channel.id || '')
          return (
            <ChannelCard
              key={name}
              channel={channel}
              guided={hasGuidedSetup(channel, specs) && !snapshotQuery.isLoading}
              busy={busyName === name}
              pairingBusy={pairingBusy}
              blocked={blocked}
              configPath={configPath}
              onConfigure={() => openSheet(name)}
              onToggle={(enabled) => toggle.mutate({ name, enabled })}
              onRestart={() => restartChannel.mutate(name)}
              onRemove={() => setRemoving(name)}
              onResolve={(senderId, approved) =>
                resolve.mutate({ channel: name, senderId, approved })
              }
              onRevoke={(senderId) => revoke.mutate({ channel: name, senderId })}
            />
          )
        })
      )}

      {sheet ? (
        <ChannelSetupSheet
          key={`${sheet.editingName ?? ''}:${sheet.nonce}`}
          editingName={sheet.editingName}
          snapshot={snapshot}
          loading={snapshotQuery.isLoading}
          loadError={snapshotQuery.isError ? errorText(snapshotQuery.error) : undefined}
          channels={channels}
          saving={save.isPending}
          saveError={saveError}
          conflict={conflict}
          onRetry={() => void snapshotQuery.refetch()}
          onReload={() => openSheet(sheet.editingName)}
          onSave={(entry) => save.mutate(entry)}
          onClose={() => setSheet(null)}
        />
      ) : null}

      {removing ? (
        <RemoveDialog
          name={removing}
          onCancel={() => setRemoving(null)}
          onConfirm={() => {
            const name = removing
            setRemoving(null)
            remove.mutate(name)
          }}
        />
      ) : null}
    </>
  )
}

function ChannelCard({
  channel,
  guided,
  busy,
  pairingBusy,
  blocked,
  configPath,
  onConfigure,
  onToggle,
  onRestart,
  onRemove,
  onResolve,
  onRevoke,
}: {
  channel: MergedChannel
  guided: boolean
  busy: boolean
  pairingBusy: boolean
  blocked: boolean
  configPath: string | null
  onConfigure: () => void
  onToggle: (enabled: boolean) => void
  onRestart: () => void
  onRemove: () => void
  onResolve: (senderId: string, approved: boolean) => void
  onRevoke: (senderId: string) => void
}) {
  const d = channelDisplay(channel)
  const type = String(channel.type || '')
  const enabled = channel.enabled !== false
  const hint = statusHint({
    status: d.status,
    isRunning: d.isRunning,
    isDead: d.isDead,
    enabled,
    name: d.name,
  })
  const since = channel.connected_since ? relTime(channel.connected_since) : '—'
  const restartable = canRestartChannel(channel)

  return (
    <Card
      title={d.name}
      icon={<AdapterMark type={type} size={36} />}
      blurb={type || t('settings.channels.type.unknown')}
      action={
        <Pill tone={statusTone(d.status)} pulse={d.status === 'restarting'}>
          {d.status}
        </Pill>
      }
      footNote={hint}
      foot={
        <>
          <Button disabled={busy || blocked} onClick={onRemove}>
            {t('settings.channels.card.remove')}
          </Button>
          {restartable ? (
            <Button disabled={busy} onClick={onRestart}>
              {busy ? t('settings.channels.card.restarting') : t('settings.channels.card.restart')}
            </Button>
          ) : null}
          {guided ? (
            <Button variant="primary" disabled={busy || blocked} onClick={onConfigure}>
              {t('settings.channels.card.configure')}
            </Button>
          ) : (
            <Button
              disabled={!isDesktop() || !configPath}
              title={t('settings.channels.card.advanced.help')}
              onClick={() => configPath && void desktopApi().app.openPath(configPath)}
            >
              {t('settings.channels.card.advanced')}
            </Button>
          )}
        </>
      }
    >
      <Row label={t('settings.channels.card.connected')}>
        <Value>{since}</Value>
      </Row>
      <Row label={t('settings.channels.card.restarts')}>
        <Value tone={Number(d.attempts) > 0 ? 'warn' : undefined}>{d.attempts}</Value>
      </Row>
      <Row
        label={t('settings.channels.card.enabled')}
        help={t('settings.channels.card.enabled.help')}
      >
        <Switch
          checked={enabled}
          disabled={busy || blocked}
          aria-label={`${t('settings.channels.card.enabled')} ${d.name}`}
          onCheckedChange={onToggle}
        />
      </Row>
      {channel.type === 'telegram' && channel.access ? (
        <AccessRows
          access={channel.access}
          busy={pairingBusy}
          onResolve={onResolve}
          onRevoke={onRevoke}
        />
      ) : null}
      <details className="stg-log chn-details">
        <summary>{t('settings.channels.card.details')}</summary>
        <pre>{d.configJson}</pre>
      </details>
    </Card>
  )
}

/** Telegram admission: who is waiting for a code to be accepted, who is in. */
function AccessRows({
  access,
  busy,
  onResolve,
  onRevoke,
}: {
  access: ChannelAccess
  busy: boolean
  onResolve: (senderId: string, approved: boolean) => void
  onRevoke: (senderId: string) => void
}) {
  const pending = Array.isArray(access.pending) ? access.pending : []
  const paired = Array.isArray(access.paired) ? access.paired : []
  const locked = isAccessLocked(access.locked_until)
  const groups = access.groups_enabled
    ? fill(
        access.group_mention_required
          ? 'settings.channels.access.groups.mention'
          : 'settings.channels.access.groups.on',
        { count: access.group_chat_ids?.length || 0 },
      )
    : t('settings.channels.access.groups.off')

  return (
    <div className="chn-access" data-pending={pending.length ? 'true' : undefined}>
      <div className="chn-access__head">
        <span className="chn-access__title">{t('settings.channels.access.title')}</span>
        <span className="chn-access__groups">{groups}</span>
      </div>
      {locked ? <Notice tone="warn">{t('settings.channels.access.locked')}</Notice> : null}
      <div className="chn-access__group">
        <span className="chn-access__label">
          {t('settings.channels.access.pending')} <span>{pending.length}</span>
        </span>
        {pending.length ? (
          pending.map((item, i) => (
            <Person key={`p-${String(item.sender_id ?? i)}`} item={item}>
              <Button
                variant="primary"
                disabled={busy || locked}
                onClick={() => onResolve(String(item.sender_id ?? ''), true)}
              >
                {t('settings.channels.access.pair')}
              </Button>
              <Button
                disabled={busy}
                onClick={() => onResolve(String(item.sender_id ?? ''), false)}
              >
                {t('settings.channels.access.deny')}
              </Button>
            </Person>
          ))
        ) : (
          <span className="chn-access__empty">{t('settings.channels.access.none')}</span>
        )}
      </div>
      <div className="chn-access__group">
        <span className="chn-access__label">
          {t('settings.channels.access.paired')} <span>{paired.length}</span>
        </span>
        {paired.length ? (
          paired.map((item, i) => (
            <Person key={`a-${String(item.sender_id ?? i)}`} item={item}>
              <Button disabled={busy} onClick={() => onRevoke(String(item.sender_id ?? ''))}>
                {t('settings.channels.access.disconnect')}
              </Button>
            </Person>
          ))
        ) : (
          <span className="chn-access__empty">{t('settings.channels.access.nonePaired')}</span>
        )}
      </div>
    </div>
  )
}

function Person({ item, children }: { item: AccessAccount; children: React.ReactNode }) {
  const meta = senderMeta(item)
  return (
    <div className="chn-person">
      <div className="chn-person__who">
        <span className="chn-person__name">{senderLabel(item)}</span>
        {meta ? <span className="chn-person__meta">{meta}</span> : null}
      </div>
      <div className="chn-person__actions">{children}</div>
    </div>
  )
}

function RemoveDialog({
  name,
  onCancel,
  onConfirm,
}: {
  name: string
  onCancel: () => void
  onConfirm: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={onCancel}
      overlayClassName="stg-confirm__overlay"
      className="stg-confirm"
    >
      <h2 id={titleId}>{fill('settings.channels.confirmRemove.title', { name })}</h2>
      <p id={bodyId}>{t('settings.channels.confirmRemove.body')}</p>
      <div className="stg-confirm__actions">
        <Button onClick={onCancel}>{t('settings.channels.cancel')}</Button>
        <Button variant="danger" onClick={onConfirm}>
          {t('settings.channels.confirmRemove.confirm')}
        </Button>
      </div>
    </ModalShell>
  )
}
