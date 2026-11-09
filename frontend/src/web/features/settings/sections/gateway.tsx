import { useQuery } from '@tanstack/react-query'
import { Eye, EyeOff, Loader2, Power, RefreshCw } from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'
import { useBootstrap, useRpc } from '@/app/providers'
import { formatNumber, t } from '@/i18n'
import '@/i18n/en/webSetSystem'
import { resolveWsUrl } from '@/lib/bootstrap'
import { readStoredWsUrl, storeToken, storeWsUrl } from '@/lib/credentials'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import type { RpcState } from '@/lib/ws-rpc'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/web/components/ui/alert-dialog'
import { Button } from '@/web/components/ui/button'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useRegisterFields, useReveal } from '../fields'

// Connections › Gateway: where this browser connects, with what, and the
// connection-level actions (new credentials, reconnect, restart).

interface GatewayStatus {
  status?: string
  version?: string
  uptime_ms?: number
  provider?: string | null
  active_sessions?: number
}

const inputClass =
  'h-9 w-full max-w-xl rounded-md border border-border-control bg-surface-0 px-3 touch:h-11 touch:text-base'

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
    <div className="grid grid-cols-[10rem_1fr] gap-2 py-1.5 max-sm:grid-cols-1 max-sm:gap-0">
      <dt className="text-text-muted">{label}</dt>
      <dd className="min-w-0 break-all">{value}</dd>
    </div>
  )
}

function ConnectionDetails({ url }: { url: string }) {
  const rpc = useRpc()
  const bootstrap = useBootstrap()
  const connection = useDisplayedConnection()
  const status = useQuery({
    queryKey: ['status'],
    queryFn: () => rpc.call<GatewayStatus>('status'),
    enabled: connection === 'connected',
    retry: false,
  })
  const s = status.data
  const dash = t('webSetSystem.none')
  return (
    <section aria-labelledby="gateway-details" className="flex flex-col gap-2">
      <h2 id="gateway-details" className="text-base font-semibold">
        {t('webSetSystem.gatewayDetails')}
      </h2>
      <dl data-field-id="gatewayUrl" className="rounded-lg border border-border px-3 py-1.5">
        <Detail label={t('webSetSystem.gatewayState')} value={stateLabel(connection)} />
        <Detail
          label={t('webSetSystem.gatewayUrl')}
          value={<span className="font-mono text-small">{url}</span>}
        />
        <Detail label={t('webSetSystem.gatewayAuth')} value={bootstrap.auth_mode || dash} />
        <Detail label={t('webSetSystem.consoleVersion')} value={bootstrap.version || dash} />
        <Detail label={t('webSetSystem.gatewayVersion')} value={s?.version || dash} />
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
        <p className="text-small text-text-muted">{t('webSetSystem.gatewayStatusFailed')}</p>
      ) : null}
    </section>
  )
}

/** New token (and optionally a new address); stored for this tab, then reconnect. */
function Credentials({ url: currentUrl, defaultUrl }: { url: string; defaultUrl: string }) {
  const rpc = useRpc()
  const tokenId = useId()
  const urlId = useId()
  const [token, setToken] = useState('')
  const [url, setUrl] = useState(currentUrl)
  const [shown, toggleShown] = useReveal()
  const [sent, setSent] = useState(false)

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (!token.trim()) return
    const target = url.trim() || defaultUrl
    storeToken(token.trim())
    storeWsUrl(target === defaultUrl ? null : target)
    useRuntimeSignals.setState({ handshake: null })
    rpc.connect(target, token.trim())
    setToken('')
    setSent(true)
  }

  return (
    <form onSubmit={onSubmit} aria-labelledby="gateway-credentials" className="flex flex-col gap-3">
      <h2 id="gateway-credentials" className="text-base font-semibold">
        {t('webSetSystem.credentials')}
      </h2>
      <div data-field-id="gatewayToken" className="flex flex-col gap-1">
        <label htmlFor={tokenId} className="font-medium">
          {t('webSetSystem.token')}
        </label>
        <p id={`${tokenId}-help`} className="text-small text-text-muted">
          {t('webSetSystem.tokenHelp')}
        </p>
        <div className="flex max-w-xl items-center gap-2">
          <input
            id={tokenId}
            aria-describedby={`${tokenId}-help`}
            type={shown ? 'text' : 'password'}
            autoComplete="off"
            value={token}
            onChange={(e) => {
              setToken(e.target.value)
              setSent(false)
            }}
            className={inputClass}
          />
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={shown ? t('webSetSystem.hide') : t('webSetSystem.reveal')}
            onClick={toggleShown}
          >
            {shown ? <EyeOff /> : <Eye />}
          </Button>
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={urlId} className="font-medium">
          {t('webSetSystem.gatewayUrl')}
        </label>
        <p id={`${urlId}-help`} className="text-small text-text-muted">
          {t('webSetSystem.urlHelp')}
        </p>
        <input
          id={urlId}
          aria-describedby={`${urlId}-help`}
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          className={`${inputClass} font-mono text-small`}
        />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" variant="primary" disabled={!token.trim()}>
          {t('webSetSystem.connect')}
        </Button>
        {!token.trim() && !sent ? (
          <span className="text-small text-text-muted">{t('webSetSystem.connectNeedsToken')}</span>
        ) : null}
        {sent ? (
          <span role="status" className="text-small text-text-muted">
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
  const [confirm, setConfirm] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [message, setMessage] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null)
  const restartGate = gate(requireConnection(connection))

  const reconnect = () => {
    setMessage({ tone: 'ok', text: t('webSetSystem.reconnecting') })
    rpc.reconnect({ force: true })
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
    <section aria-labelledby="gateway-actions" className="flex flex-col gap-3">
      <h2 id="gateway-actions" className="text-base font-semibold">
        {t('webSetSystem.gatewayActions')}
      </h2>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" onClick={reconnect}>
          <RefreshCw aria-hidden />
          {t('webSetSystem.reconnect')}
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
        <p
          role={message.tone === 'error' ? 'alert' : 'status'}
          className={message.tone === 'error' ? 'text-small text-danger' : 'text-small'}
        >
          {message.text}
        </p>
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
  const bootstrap = useBootstrap()
  const defaultUrl = resolveWsUrl(bootstrap.ws_url)
  const url = readStoredWsUrl() || defaultUrl

  useRegisterFields('gateway', [
    { id: 'gatewayUrl', label: t('webSetSystem.gatewayUrl') },
    { id: 'gatewayToken', label: t('webSetSystem.token') },
  ])

  return (
    <div className="flex flex-col gap-8">
      <ConnectionDetails url={url} />
      <Credentials url={url} defaultUrl={defaultUrl} />
      <Actions />
    </div>
  )
}
