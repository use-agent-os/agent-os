import './channels.css'
import { useEffect } from 'react'
import { useNavigate } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCwIcon, SettingsIcon } from 'lucide-react'
import { toast } from 'sonner'
import { AsciiField } from '@/components/AsciiField'
import { CommandLine } from '@/components/CommandLine'
import { Button } from '@/components/ui/button'
import { useRpc } from '@/app/providers'
import { relTime } from '@/views/overview/logic'
import {
  channelDisplay,
  channelStats,
  inactiveHint,
  isAccessLocked,
  mergeChannels,
  resolveAccessMode,
  senderLabel,
  senderMeta,
  sortChannels,
  statusHint,
  type AccessAccount,
  type ChannelAccess,
  type MergedChannel,
  type RawChannel,
  type Tone,
} from './logic'

// channels.js:60-61 — the 5s reload cadence (react-query refetchInterval, the
// single source of truth alongside the channel.status event-driven invalidate).
const POLL_MS = 5000

// channels.js:74-83 — the two parallel reads. access.list is tolerant of
// failure so a broken access endpoint never blocks the channel status render.
interface ChannelsStatus {
  channels?: RawChannel[]
}
interface AccessList {
  channels?: ChannelAccess[]
}

// Tone token → tone-* class (status color ONLY via --tone; never hardcoded).
function toneClass(tone: Tone): string {
  return tone === 'danger' ? 'tone-danger' : tone === 'ok' ? 'tone-ok' : 'tone-dim'
}

// channels.js:262-267 — the four selectable Telegram DM access modes.
const ACCESS_MODE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'pairing', label: 'Pairing codes' },
  { value: 'allowlist', label: 'Allowlist only' },
  { value: 'open', label: 'Open to everyone' },
  { value: 'disabled', label: 'Disabled' },
]

function PersonRow({
  item,
  variant,
  disabled,
  onApprove,
  onDeny,
  onRevoke,
}: {
  item: AccessAccount
  variant: 'pending' | 'approved'
  disabled: boolean
  onApprove?: () => void
  onDeny?: () => void
  onRevoke?: () => void
}) {
  // channels.js:292-316 — one Telegram account row (pending: Approve/Deny;
  // approved: Revoke).
  return (
    <div className="ch-access__person">
      <div className="ch-access__identity">
        <strong>{senderLabel(item)}</strong>
        <span>{senderMeta(item)}</span>
        {variant === 'pending' && item.code ? (
          <code className="ch-access__code">{String(item.code)}</code>
        ) : null}
      </div>
      {variant === 'pending' ? (
        <div className="ch-access__person-actions">
          <Button type="button" size="sm" disabled={disabled} onClick={onApprove}>
            Approve
          </Button>
          <Button
            type="button"
            size="sm"
            variant="destructive"
            disabled={disabled}
            onClick={onDeny}
          >
            Deny
          </Button>
        </div>
      ) : (
        <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={onRevoke}>
          Revoke
        </Button>
      )}
    </div>
  )
}

function AccessPanel({
  channel,
  busy,
  onSetMode,
  onResolve,
  onRevoke,
}: {
  channel: MergedChannel
  busy: boolean
  onSetMode: (channel: string, mode: string) => void
  onResolve: (channel: string, senderId: string, approved: boolean) => void
  onRevoke: (channel: string, senderId: string) => void
}) {
  // channels.js:246-290 — telegram-only access panel.
  const access = channel.access
  if (!access || channel.type !== 'telegram') return null
  const channelName = String(channel.name || '')
  const pending = Array.isArray(access.pending) ? access.pending : []
  const approved = Array.isArray(access.approved) ? access.approved : []
  const mode = resolveAccessMode(access.mode)
  const locked = isAccessLocked(access.locked_until)
  const groupMode = access.group_mode || 'allowlist'

  return (
    <section className={`ch-access${pending.length ? ' ch-access--pending' : ''}`}>
      <div className="ch-access__head">
        <div>
          <span className="ch-access__eyebrow t-label">Telegram accounts</span>
          <h4 className="ch-access__title">Chat access</h4>
        </div>
        <label className="ch-access__mode">
          <span className="t-label">Mode</span>
          <select
            className="ch-access__select t-data"
            aria-label="Telegram chat access mode"
            value={mode}
            disabled={busy}
            onChange={(e) => onSetMode(channelName, e.target.value)}
          >
            {ACCESS_MODE_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {locked ? (
        <p className="ch-access__warning">
          Pairing approval is locked for one hour after repeated invalid codes.
        </p>
      ) : null}

      {mode === 'open' ? (
        <p className="ch-access__note">
          Every Telegram account can DM this bot. Group access remains separately controlled as{' '}
          {groupMode}.
        </p>
      ) : mode === 'disabled' ? (
        <p className="ch-access__note">
          Telegram direct messages are disabled. Group access remains separately controlled as{' '}
          {groupMode}.
        </p>
      ) : (
        <>
          <div className="ch-access__group">
            <div className="ch-access__group-title t-label">
              Pending <span>{pending.length}</span>
            </div>
            {pending.length ? (
              <div className="ch-access__people">
                {pending.map((item, i) => (
                  <PersonRow
                    key={`pending-${String(item.sender_id ?? i)}`}
                    item={item}
                    variant="pending"
                    disabled={busy}
                    onApprove={() => onResolve(channelName, String(item.sender_id ?? ''), true)}
                    onDeny={() => onResolve(channelName, String(item.sender_id ?? ''), false)}
                  />
                ))}
              </div>
            ) : (
              <p className="ch-access__empty">No Telegram accounts are waiting for approval.</p>
            )}
          </div>
          <div className="ch-access__group">
            <div className="ch-access__group-title t-label">
              Approved <span>{approved.length}</span>
            </div>
            {approved.length ? (
              <div className="ch-access__people">
                {approved.map((item, i) => (
                  <PersonRow
                    key={`approved-${String(item.sender_id ?? i)}`}
                    item={item}
                    variant="approved"
                    disabled={busy}
                    onRevoke={() => onRevoke(channelName, String(item.sender_id ?? ''))}
                  />
                ))}
              </div>
            ) : (
              <p className="ch-access__empty">No approved accounts yet.</p>
            )}
          </div>
        </>
      )}
    </section>
  )
}

function ChannelCard({
  channel,
  busy,
  onSetMode,
  onResolve,
  onRevoke,
}: {
  channel: MergedChannel
  busy: boolean
  onSetMode: (channel: string, mode: string) => void
  onResolve: (channel: string, senderId: string, approved: boolean) => void
  onRevoke: (channel: string, senderId: string) => void
}) {
  // channels.js:202-241 — one channel card.
  const d = channelDisplay(channel)
  const since = channel.connected_since ? relTime(channel.connected_since) : '—'
  const hint = statusHint({
    status: d.status,
    isRunning: d.isRunning,
    isDead: d.isDead,
    enabled: channel.enabled !== false,
    name: d.name,
  })
  return (
    <article className={`panel ch-card ${toneClass(d.tone)}`} aria-label={`Channel ${d.name}`}>
      <header className="ch-card__head">
        <span
          className={`ch-card__dot tone-${d.tone === 'danger' ? 'danger' : d.tone === 'ok' ? 'ok' : 'dim'}`}
          aria-hidden="true"
        />
        <span className="ch-card__name" title={d.name}>
          {d.name}
        </span>
        <span className="ch-card__type t-data">{channel.type || 'unknown'}</span>
      </header>
      <div className="ch-card__status">
        <span className={`ch-card__chip t-data ${toneClass(d.tone)}`}>{d.status}</span>
      </div>
      <dl className="ch-card__meta">
        <div>
          <dt className="t-label">Connected</dt>
          <dd className="t-data">{since}</dd>
        </div>
        <div>
          <dt className="t-label">Restart attempts</dt>
          <dd className="t-data">{d.attempts}</dd>
        </div>
      </dl>
      <AccessPanel
        channel={channel}
        busy={busy}
        onSetMode={onSetMode}
        onResolve={onResolve}
        onRevoke={onRevoke}
      />
      <details className="ch-card__config">
        <summary>Adapter config</summary>
        <pre className="ch-card__config-pre t-data">{d.configJson}</pre>
      </details>
      <footer className="ch-card__footnote">
        <span>{hint}</span>
      </footer>
    </article>
  )
}

function StatTile({
  label,
  value,
  hint,
  hero,
  attention,
}: {
  label: string
  value: React.ReactNode
  hint: React.ReactNode
  hero?: boolean
  attention?: boolean
}) {
  // channels.js:146,384-389 — the attention tile is warn-toned via
  // .ch-stat--attention (--warn), NOT a per-tile --tone; there is no danger
  // tone on a stat tile in the legacy contract.
  return (
    <div
      className={`ch-stat${hero ? ' ch-stat--hero' : ''}${attention ? ' ch-stat--attention' : ''}`}
      aria-label={label}
    >
      <span className="ch-stat__label t-label">{label}</span>
      <strong className="ch-stat__value t-data">{value}</strong>
      <span className="ch-stat__hint">{hint}</span>
    </div>
  )
}

export function ChannelsPage() {
  const rpc = useRpc()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  useEffect(() => {
    document.title = 'Channels - AgentOS Control'
  }, [])

  // channels.js:74-108 — two parallel reads; access.list tolerates failure.
  const channelsQuery = useQuery<MergedChannel[]>({
    queryKey: ['channels'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const [status, access] = await Promise.all([
        rpc.call<ChannelsStatus>('channels.status', {}),
        rpc
          .call<AccessList>('channels.access.list', {})
          .catch(() => ({ channels: [] }) as AccessList),
      ])
      return sortChannels(mergeChannels(status.channels, access.channels))
    },
    refetchInterval: POLL_MS,
    refetchOnWindowFocus: false,
  })

  // channels.js:107 — load-failure toast (stable id so repeats dedupe).
  useEffect(() => {
    if (channelsQuery.isError) {
      const err = channelsQuery.error
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to load channels: ' + message, { id: 'channels-load-err' })
    }
  }, [channelsQuery.isError, channelsQuery.error])

  // channels.js:55-56,64-72 — subscribe to real-time channel.status events and
  // invalidate the channels query (targeted refetch); cleaned up on unmount.
  useEffect(() => {
    const unsub = rpc.on('channel.status', () => {
      void queryClient.invalidateQueries({ queryKey: ['channels'] })
    })
    return () => {
      unsub()
    }
  }, [rpc, queryClient])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['channels'] })

  // channels.js:319-334 — mode select mutation (open warns; else info) + refetch.
  const setModeMutation = useMutation({
    mutationFn: (vars: { channel: string; mode: string }) =>
      rpc.call('channels.access.setMode', vars),
    onSuccess: (_data, vars) => {
      const message = 'Telegram DM policy: ' + vars.mode + '.'
      if (vars.mode === 'open') toast.warning(message, { id: 'channels-mode' })
      else toast.info(message, { id: 'channels-mode' })
      void invalidate()
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to update access mode: ' + message, { id: 'channels-mode-err' })
      void invalidate()
    },
  })

  // channels.js:336-353 — approve/deny mutation + refetch.
  const resolveMutation = useMutation({
    mutationFn: (vars: { channel: string; senderId: string; approved: boolean }) =>
      rpc.call('channels.access.resolve', vars),
    onSuccess: (_data, vars) => {
      if (vars.approved) toast.info('Telegram account approved.', { id: 'channels-resolve' })
      else toast.warning('Telegram account denied.', { id: 'channels-resolve' })
      void invalidate()
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to resolve account request: ' + message, { id: 'channels-resolve-err' })
    },
  })

  // channels.js:355-370 — revoke mutation + refetch.
  const revokeMutation = useMutation({
    mutationFn: (vars: { channel: string; senderId: string }) =>
      rpc.call('channels.access.revoke', vars),
    onSuccess: () => {
      toast.info('Telegram account access revoked.', { id: 'channels-revoke' })
      void invalidate()
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to revoke account: ' + message, { id: 'channels-revoke-err' })
    },
  })

  const busy = setModeMutation.isPending || resolveMutation.isPending || revokeMutation.isPending

  const channels = channelsQuery.data ?? []
  const stats = channelStats(channels)

  const onSetMode = (channel: string, mode: string) => setModeMutation.mutate({ channel, mode })
  const onResolve = (channel: string, senderId: string, approved: boolean) =>
    resolveMutation.mutate({ channel, senderId, approved })
  const onRevoke = (channel: string, senderId: string) =>
    revokeMutation.mutate({ channel, senderId })

  return (
    <div className="ch-stage">
      <header className="ch-stage__header">
        <AsciiField />
        <div className="ch-stage__title-block">
          <span className="t-label">Control · Channels</span>
          <h2 className="t-display">Channels</h2>
          <p className="ch-stage__subtitle">
            Runtime status for configured channels, with account approvals for Telegram bots.
          </p>
        </div>
        <Button
          variant="outline"
          title="Refresh"
          className="ch-stage__refresh text-xs uppercase tracking-[0.14em]"
          onClick={() => void invalidate()}
        >
          <RefreshCwIcon />
          <span>Refresh</span>
        </Button>
      </header>

      <section className="ch-stats" aria-label="Channels summary">
        <StatTile
          label="Total channels"
          hero
          value={stats.total}
          hint={`${stats.typeCount} type${stats.typeCount === 1 ? '' : 's'}`}
        />
        <StatTile
          label="Connected"
          value={stats.connected}
          hint={
            stats.connected ? 'live' : stats.attention ? `${stats.attention} unhealthy` : 'all idle'
          }
        />
        <StatTile
          label="Inactive"
          value={stats.inactive}
          hint={
            stats.attention ? (
              // channels.js:139 — legacy wraps this hint in .ch-neg (--danger).
              <span className="ch-neg">{stats.attention} need attention</span>
            ) : (
              inactiveHint(stats.inactive, stats.disabled)
            )
          }
        />
        <StatTile label="Restart attempts" value={stats.restarts} hint="since gateway start" />
        <StatTile
          label="Chat approvals"
          value={stats.pendingAccess}
          attention={stats.pendingAccess > 0}
          hint={stats.pendingAccess ? 'Telegram account requests' : 'no accounts waiting'}
        />
      </section>

      <section className="ch-list">
        <div className="ch-list__head">
          <h3 className="ch-list__title t-label">
            Configured channels{' '}
            {channels.length ? (
              <span className="ch-list__count t-data">{channels.length}</span>
            ) : null}
          </h3>
        </div>

        {channels.length === 0 ? (
          <div className="ch-empty">
            <div className="ch-empty__title">No configured channels.</div>
            <p className="ch-empty__msg">
              Channel provisioning stays in guided setup and the CLI so credentials, dependency
              extras, webhook URLs, and restart requirements stay explicit.
            </p>
            <div className="ch-empty__actions">
              <Button type="button" onClick={() => navigate('/setup')}>
                <SettingsIcon />
                <span>Guided setup</span>
              </Button>
            </div>
            <div className="ch-empty__commands">
              <CommandLine
                command="agentos onboard configure channels"
                toastIdPrefix="channels-copy"
              />
              <CommandLine command="agentos channels list" toastIdPrefix="channels-copy" />
            </div>
          </div>
        ) : (
          <div className="ch-cards">
            {channels.map((channel, i) => (
              <ChannelCard
                key={String(channel.name || channel.id || i)}
                channel={channel}
                busy={busy}
                onSetMode={onSetMode}
                onResolve={onResolve}
                onRevoke={onRevoke}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
