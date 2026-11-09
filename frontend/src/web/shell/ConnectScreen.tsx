import { KeyRound } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useBootstrap, useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webConnection'
import { resolveWsUrl } from '@/lib/bootstrap'
import { readStoredWsUrl, storeToken, storeWsUrl } from '@/lib/credentials'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { useConnection } from '@/stores/connection'
import { Button } from '@/web/components/ui/button'
import { useConnectAttempt, type ConnectReason } from './connection-gate'

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

/**
 * Full-page Connect screen (connection spec "Connect screen"). It replaces
 * the workspace, so nothing behind it renders or requests data.
 */
export function ConnectScreen({ reason }: { reason: ConnectReason }) {
  const rpc = useRpc()
  const bootstrap = useBootstrap()
  const connecting = useConnection((s) => s.state === 'connecting') && reason === 'connecting'
  const defaultUrl = readStoredWsUrl() || resolveWsUrl(bootstrap.ws_url)
  const [url, setUrl] = useState(defaultUrl)
  const [token, setToken] = useState('')
  const rateLimited = reason === 'auth_rate_limited'

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (rateLimited || !token.trim()) return
    const target = url.trim() || defaultUrl
    storeToken(token.trim())
    storeWsUrl(target === resolveWsUrl(bootstrap.ws_url) ? null : target)
    useRuntimeSignals.setState({ handshake: null })
    useConnectAttempt.getState().set(true)
    rpc.connect(target, token.trim())
  }

  const error =
    reason === 'auth_failed'
      ? t('webConnection.connectRejected')
      : rateLimited
        ? t('webConnection.connectRateLimited')
        : null

  return (
    <main
      id="main-content"
      className="flex min-h-dvh items-center justify-center bg-surface-0 p-4 text-text"
    >
      <form
        onSubmit={onSubmit}
        aria-labelledby="connect-title"
        className="flex w-full max-w-md flex-col gap-4 rounded-xl border border-border bg-surface-1 p-6 shadow-2"
      >
        <div className="flex items-center gap-3">
          <KeyRound className="size-6 text-accent" aria-hidden />
          <h1 id="connect-title" className="text-lg font-semibold">
            {t('webConnection.connectTitle')}
          </h1>
        </div>
        <p className="text-text-muted">{t('webConnection.connectBody')}</p>
        <p className="text-small text-text-muted">
          {t('webConnection.connectAddress', { address: hostOf(url || defaultUrl) })}
        </p>
        {error ? (
          <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2">
            {error}
          </p>
        ) : null}
        <div className="flex flex-col gap-1">
          <label htmlFor="connect-token" className="font-medium">
            {t('webConnection.connectToken')}
          </label>
          <input
            id="connect-token"
            aria-describedby="connect-token-help"
            type="password"
            autoComplete="off"
            autoFocus
            value={token}
            onChange={(e) => setToken(e.target.value)}
            disabled={rateLimited}
            className="h-10 rounded-md border border-border-control bg-surface-0 px-3"
          />
          <span id="connect-token-help" className="text-small text-text-muted">
            {t('webConnection.connectTokenStored')}
          </span>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="connect-url" className="font-medium">
            {t('webConnection.connectUrl')}
          </label>
          <input
            id="connect-url"
            aria-describedby="connect-url-help"
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={rateLimited}
            className="h-10 rounded-md border border-border-control bg-surface-0 px-3 font-mono text-small"
          />
          <span id="connect-url-help" className="text-small text-text-muted">
            {t('webConnection.connectUrlHelp')}
          </span>
        </div>
        <p className="text-small text-text-muted">{t('webConnection.connectWhere')}</p>
        <Button
          type="submit"
          variant="primary"
          disabled={rateLimited || !token.trim() || reason === 'connecting'}
        >
          {reason === 'connecting' || connecting
            ? t('webConnection.connectConnecting')
            : t('webConnection.connectSubmit')}
        </Button>
      </form>
    </main>
  )
}
