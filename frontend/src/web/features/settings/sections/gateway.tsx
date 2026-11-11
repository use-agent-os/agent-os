import { useQuery } from '@tanstack/react-query'
import {
  CircleAlert,
  CircleCheck,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Plug,
  Power,
  RefreshCw,
  Server,
  Unplug,
} from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'
import { useBootstrap, useRpc } from '@/app/providers'
import { formatNumber, t } from '@/i18n'
import '@/i18n/en/webConnection'
import '@/i18n/en/webSetSystem'
import { resolveWsUrl } from '@/lib/bootstrap'
import { readStoredWsUrl } from '@/lib/credentials'
import { gate, requireConnection } from '@/lib/disabled-reason'
import type { RpcState } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/web/components/ui/alert-dialog'
import { StatusDot } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Input } from '@/web/components/ui/input'
import { connectGateway, useConnectAttempt } from '@/web/shell/connection-gate'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useRegisterFields, useReveal } from '../fields'
import { Notice } from '../ui'

// Connections › Gateway: where this browser connects, with what, and the
// connection-level actions (new credentials, reconnect, restart).

interface GatewayStatus {
  status?: string
  version?: string
  uptime_ms?: number
  provider?: string | null
  active_sessions?: number
}

function stateLabel(state: RpcState): string {
  if (state === 'connected') return t('webSetSystem.gatewayConnected')
  if (state === 'connecting') return t('webSetSystem.gatewayConnecting')
  return t('webSetSystem.gatewayDisconnected')
}

function formatUptime(ms: number): string {
  const minutes = Math.floor(ms / 60_000)
  const hours = Math.floor(minutes / 60)
  const days = Math.floor(hours / 24)
  if (days > 0) return t('webSetSystem.uptimeDays', { days, hours: hours % 24 })
  if (hours > 0) return t('webSetSystem.uptimeHours', { hours, minutes: minutes % 60 })
  return t('webSetSystem.uptimeMinutes', { minutes })
}

function Detail({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[11rem_minmax(0,1fr)] items-baseline gap-3 px-5 py-2.5 max-sm:grid-cols-1 max-sm:gap-0.5">
      <dt className="text-small text-text-muted">{label}</dt>
      <dd className="min-w-0 break-all tabular-nums">{value}</dd>
    </div>
  )
}

const STATE_TONE = { connected: 'ok', connecting: 'warn', disconnected: 'danger' } as const

function ConnectionDetails({ url }: { url: string }) {
  const rpc = useRpc()
  const bootstrap = useBootstrap()
  const connection = useDisplayedConnection()
  const status = useQuery({
    // Per address: after a switch the details describe the new gateway.
    queryKey: ['status', url],
    queryFn: () => rpc.call<GatewayStatus>('status'),
    enabled: connection === 'connected',
    retry: false,
  })
  const s = status.data
  const dash = t('webSetSystem.none')
  return (
    <section aria-labelledby="gateway-details" className="set-card px-0 pb-1">
      <header className="flex flex-wrap items-center gap-3 px-5 pb-2">
        <span
          aria-hidden
          className="grid size-8 shrink-0 place-items-center rounded-control bg-surface-2 text-text-muted shadow-highlight"
        >
          <Server className="size-4" />
        </span>
        <h2 id="gateway-details" className="set-card-title min-w-0 flex-1">
          {t('webSetSystem.gatewayDetails')}
        </h2>
      </header>
      <dl data-field-id="gatewayUrl" className="divide-y divide-border border-t border-border">
        <Detail
          label={t('webSetSystem.gatewayState')}
          value={
            <span className="inline-flex items-center gap-2">
              <StatusDot
                tone={STATE_TONE[connection as keyof typeof STATE_TONE] ?? 'neutral'}
                live={connection === 'connected'}
                className="size-2"
              />
              {stateLabel(connection)}
            </span>
          }
        />
        <Detail
          label={t('webSetSystem.gatewayUrl')}
          value={<span className="set-key">{url}</span>}
        />
        <Detail label={t('webSetSystem.gatewayAuth')} value={bootstrap.auth_mode || dash} />
        <Detail
          label={t('webSetSystem.consoleVersion')}
          value={<span className="font-mono text-small">{bootstrap.version || dash}</span>}
        />
        <Detail
          label={t('webSetSystem.gatewayVersion')}
          value={<span className="font-mono text-small">{s?.version || dash}</span>}
        />
        <Detail
          label={t('webSetSystem.gatewayUptime')}
          value={s?.uptime_ms ? formatUptime(s.uptime_ms) : dash}
        />
        <Detail label={t('webSetSystem.gatewayProvider')} value={s?.provider || dash} />
        <Detail
          label={t('webSetSystem.gatewaySessions')}
          value={s?.active_sessions !== undefined ? formatNumber(s.active_sessions) : dash}
        />
      </dl>
      {status.isError ? (
        <p className="border-t border-border px-5 pt-2.5 pb-1.5 text-small text-text-muted">
          {t('webSetSystem.gatewayStatusFailed')}
        </p>
      ) : null}
    </section>
  )
}

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

/**
 * New token and/or a new address: the token is stored for this tab at once,
 * the address only once that gateway answers. Without token auth on this
 * gateway the token is optional, so the address alone can be changed.
 */
function Credentials({ url: currentUrl, defaultUrl }: { url: string; defaultUrl: string }) {
  const rpc = useRpc()
  const bootstrap = useBootstrap()
  const tokenId = useId()
  const urlId = useId()
  const [token, setToken] = useState('')
  const [url, setUrl] = useState(currentUrl)
  const [shown, toggleShown] = useReveal()
  const [sent, setSent] = useState<string | null>(null)
  const attemptUrl = useConnectAttempt((s) => s.url)
  const unreachable = useConnectAttempt((s) => s.unreachable)
  const connection = useConnection((s) => s.state)
  const tokenRequired = bootstrap.auth_mode === 'token'
  const failed = sent !== null && attemptUrl === sent && unreachable
  // The attempt is over once that gateway answers (connectGateway forgets
  // its url on the handshake).
  const joined = sent !== null && attemptUrl !== sent && connection === 'connected'

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (tokenRequired && !token.trim()) return
    const target = url.trim() || defaultUrl
    connectGateway(rpc, {
      url: target,
      token: token.trim() || null,
      defaultUrl,
      holdScreen: false,
    })
    setToken('')
    setSent(target)
  }

  return (
    <form onSubmit={onSubmit} aria-labelledby="gateway-credentials" className="set-card">
      <header className="mb-1 flex items-start gap-3">
        <span
          aria-hidden
          className="grid size-8 shrink-0 place-items-center rounded-control bg-surface-2 text-text-muted shadow-highlight"
        >
          <KeyRound className="size-4" />
        </span>
        <h2 id="gateway-credentials" className="set-card-title mt-1">
          {t('webSetSystem.credentials')}
        </h2>
      </header>
      <div data-field-id="gatewayToken" className="set-row">
        <label htmlFor={tokenId} className="set-row-label">
          {t('webSetSystem.token')}
        </label>
        <p id={`${tokenId}-help`} className="set-row-help">
          {t('webSetSystem.tokenHelp')}
          {tokenRequired ? null : ` ${t('webConnection.settingsTokenOptional')}`}
        </p>
        <div className="set-row-control">
          <span className="relative block min-w-0">
            <Input
              id={tokenId}
              aria-describedby={`${tokenId}-help`}
              type={shown ? 'text' : 'password'}
              autoComplete="off"
              value={token}
              onChange={(e) => {
                setToken(e.target.value)
                setSent(null)
              }}
              className="pr-10"
            />
            <Button
              size="icon-sm"
              variant="ghost"
              className="absolute top-1/2 right-1 -translate-y-1/2 text-text-muted hover:text-text"
              aria-label={shown ? t('webSetSystem.hide') : t('webSetSystem.reveal')}
              aria-pressed={shown}
              onClick={toggleShown}
            >
              {shown ? <EyeOff /> : <Eye />}
            </Button>
          </span>
        </div>
      </div>
      <div className="set-row" data-layout="stacked">
        <label htmlFor={urlId} className="set-row-label">
          {t('webSetSystem.gatewayUrl')}
        </label>
        <p id={`${urlId}-help`} className="set-row-help">
          {t('webSetSystem.urlHelp')}
        </p>
        <div className="set-row-control">
          <Input
            id={urlId}
            aria-describedby={`${urlId}-help`}
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            className="font-mono text-small"
          />
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-border pt-4">
        <Button type="submit" variant="primary" disabled={tokenRequired && !token.trim()}>
          <Plug aria-hidden />
          {t('webSetSystem.connect')}
        </Button>
        {tokenRequired && !token.trim() && sent === null ? (
          <span className="text-small text-text-muted">{t('webSetSystem.connectNeedsToken')}</span>
        ) : null}
        {joined ? (
          <span
            role="status"
            className="anim-fade-in inline-flex items-center gap-1.5 text-small text-ok"
          >
            <CircleCheck className="size-4" aria-hidden data-status-icon />
            {t('webSetSystem.connectDone', { host: hostOf(sent) })}
          </span>
        ) : failed ? (
          <span
            role="alert"
            className="anim-fade-in inline-flex items-center gap-1.5 text-small text-danger"
          >
            <CircleAlert className="size-4" aria-hidden data-status-icon />
            {t('webConnection.settingsUnreachable', { host: hostOf(sent) })}
          </span>
        ) : sent !== null ? (
          <span
            role="status"
            className="inline-flex items-center gap-1.5 text-small text-text-muted"
          >
            <Loader2 className="size-4 animate-spin" aria-hidden />
            {t('webSetSystem.connectSent')}
          </span>
        ) : null}
      </div>
    </form>
  )
}

function Actions() {
  const rpc = useRpc()
  const connection = useDisplayedConnection()
  const offline = useConnection((s) => s.state === 'disconnected')
  const [confirm, setConfirm] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [message, setMessage] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null)
  const restartGate = gate(requireConnection(connection))

  const reconnect = () => {
    setMessage({ tone: 'ok', text: t('webSetSystem.reconnecting') })
    rpc.reconnect({ force: true })
  }

  // Stays down until Reconnect (or a reload): the client stops retrying.
  const disconnect = () => {
    rpc.disconnect()
    setMessage({ tone: 'ok', text: t('webConnection.disconnected') })
  }

  const restart = async () => {
    setRestarting(true)
    setMessage(null)
    try {
      await rpc.call('gateway.restart', { reason: 'settings' })
      setConfirm(false)
      setMessage({ tone: 'ok', text: t('webSetSystem.restartRequested') })
    } catch (e) {
      setMessage({
        tone: 'error',
        text: t('webSetSystem.restartFailed', {
          message: e instanceof Error ? e.message : String(e),
        }),
      })
      setConfirm(false)
    } finally {
      setRestarting(false)
    }
  }

  return (
    <section aria-labelledby="gateway-actions" className="set-card flex flex-col gap-3">
      <header className="flex items-start gap-3">
        <span
          aria-hidden
          className="grid size-8 shrink-0 place-items-center rounded-control bg-surface-2 text-text-muted shadow-highlight"
        >
          <Power className="size-4" />
        </span>
        <h2 id="gateway-actions" className="set-card-title mt-1">
          {t('webSetSystem.gatewayActions')}
        </h2>
      </header>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" onClick={reconnect}>
          <RefreshCw aria-hidden />
          {t('webSetSystem.reconnect')}
        </Button>
        <Button variant="outline" disabled={offline} onClick={disconnect}>
          <Unplug aria-hidden />
          {t('webConnection.disconnect')}
        </Button>
        <Button
          variant="outline"
          disabled={restartGate.disabled || restarting}
          onClick={() => setConfirm(true)}
        >
          {restarting ? <Loader2 className="animate-spin" aria-hidden /> : <Power aria-hidden />}
          {t('webSetSystem.restart')}
        </Button>
        {restartGate.reason ? (
          <span className="text-small text-text-muted">{restartGate.reason}</span>
        ) : null}
      </div>
      {message ? (
        <Notice
          role={message.tone === 'error' ? 'alert' : 'status'}
          tone={message.tone === 'error' ? 'danger' : 'info'}
          className="anim-fade-in text-small"
        >
          {message.text}
        </Notice>
      ) : null}
      <AlertDialog open={confirm} onOpenChange={setConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('webSetSystem.restartTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('webSetSystem.restartBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webSetSystem.cancel')}</AlertDialogCancel>
            <Button
              variant="danger"
              disabled={restarting || restartGate.disabled}
              onClick={() => void restart()}
            >
              {restarting ? <Loader2 className="animate-spin" aria-hidden /> : null}
              {t('webSetSystem.restartConfirm')}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  )
}

export function GatewaySection() {
  const rpc = useRpc()
  const bootstrap = useBootstrap()
  // Re-read the address whenever the connection changes (a switch).
  useConnection((s) => s.state)
  const defaultUrl = resolveWsUrl(bootstrap.ws_url)
  // The address this tab is using; the saved one only changes once a new
  // address has connected.
  const url = (rpc as { currentUrl?: string }).currentUrl || readStoredWsUrl() || defaultUrl

  useRegisterFields('gateway', [
    { id: 'gatewayUrl', label: t('webSetSystem.gatewayUrl') },
    { id: 'gatewayToken', label: t('webSetSystem.token') },
  ])

  return (
    <div className="anim-stagger flex flex-col gap-4">
      <ConnectionDetails url={url} />
      <Credentials url={url} defaultUrl={defaultUrl} />
      <Actions />
    </div>
  )
}
