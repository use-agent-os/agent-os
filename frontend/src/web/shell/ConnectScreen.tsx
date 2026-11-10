import { KeyRound } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useBootstrap, useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webConnection'
import { resolveWsUrl } from '@/lib/bootstrap'
import { readStoredToken, readStoredWsUrl } from '@/lib/credentials'
import { Button } from '@/web/components/ui/button'
import { connectGateway, useConnectAttempt, type ConnectReason } from './connection-gate'

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
  const pageUrl = resolveWsUrl(bootstrap.ws_url)
  const attemptUrl = useConnectAttempt((s) => s.url)
  const attemptActive = useConnectAttempt((s) => s.active)
  const unreachable = useConnectAttempt((s) => s.unreachable)
  // The gateway being tried (a confirmed link, a submitted address), else the saved one.
  const target = attemptUrl || readStoredWsUrl() || pageUrl
  const [url, setUrl] = useState(target)
  const [token, setToken] = useState('')
  const [shownTarget, setShownTarget] = useState(target)
  // The target moved under the form (a gateway link was confirmed): show it,
  // and drop a token typed for the previous address.
  if (target !== shownTarget) {
    setShownTarget(target)
    if (target !== url.trim()) {
      setUrl(target)
      setToken('')
    }
  }
  const rateLimited = reason === 'auth_rate_limited'
  const tokenRequired =
    reason === 'token_required' || reason === 'auth_failed' || bootstrap.auth_mode === 'token'
  const inFlight = reason === 'connecting' && attemptActive && !unreachable

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (rateLimited || (tokenRequired && !token.trim())) return
    connectGateway(rpc, {
      url: url.trim() || target,
      token: token.trim() || null,
      defaultUrl: pageUrl,
      holdScreen: true,
    })
  }

  // A refusal with no token sent is the gateway asking for one, not a rejection.
  const rejected = reason === 'auth_failed' && !!readStoredToken()
  const error = rejected
    ? t('webConnection.connectRejected')
    : rateLimited
      ? t('webConnection.connectRateLimited')
      : reason === 'connecting' && unreachable
        ? t('webConnection.connectUnreachable', { host: hostOf(attemptUrl || url) })
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
        <p className="text-text-muted">
          {reason === 'connecting'
            ? t('webConnection.connectBodyConnecting')
            : t('webConnection.connectBody')}
        </p>
        <p className="text-small text-text-muted">
          {t('webConnection.connectAddress', { address: hostOf(url || target) })}
        </p>
        {error ? (
          <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2">
            {error}
          </p>
        ) : inFlight ? (
          <p role="status" className="text-small text-text-muted">
            {t('webConnection.connectReaching', { host: hostOf(attemptUrl || url) })}
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
            {tokenRequired
              ? t('webConnection.connectTokenStored')
              : `${t('webConnection.connectTokenStored')} ${t('webConnection.connectTokenOptional')}`}
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
          disabled={rateLimited || (tokenRequired && !token.trim())}
        >
          {t('webConnection.connectSubmit')}
        </Button>
      </form>
    </main>
  )
}
