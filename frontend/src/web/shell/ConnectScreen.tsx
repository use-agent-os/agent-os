import { AlertTriangle, ArrowRight, Globe, KeyRound, Loader2 } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useBootstrap, useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webConnection'
import { resolveWsUrl } from '@/lib/bootstrap'
import { readStoredToken, readStoredWsUrl } from '@/lib/credentials'
import { Button } from '@/web/components/ui/button'
import { Input } from '@/web/components/ui/input'
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
      className="bg-atmosphere relative flex min-h-dvh items-center justify-center overflow-hidden bg-surface-0 p-4 text-text"
    >
      {/* A faint grid fading out from the card: depth behind the first screen. */}
      <div
        aria-hidden
        data-decorative
        className="pointer-events-none absolute inset-0 [background-image:linear-gradient(var(--border)_1px,transparent_1px),linear-gradient(90deg,var(--border)_1px,transparent_1px)] [background-size:44px_44px] [mask-image:radial-gradient(ellipse_at_center,black,transparent_68%)]"
      />
      <form
        onSubmit={onSubmit}
        aria-labelledby="connect-title"
        className="anim-enter relative flex w-full max-w-[420px] flex-col gap-5 rounded-2xl border border-border bg-surface-1 p-7 shadow-overlay max-sm:p-5"
      >
        <div className="flex flex-col items-start gap-4">
          <span
            aria-hidden
            className="anim-scale-in grid size-11 place-items-center rounded-xl border border-border-strong bg-surface-2 shadow-[var(--highlight),0_0_0_6px_var(--accent-tint),0_8px_24px_-8px_var(--accent-glow)]"
          >
            <span className="shell-brand-mark size-6" />
          </span>
          <div className="flex flex-col gap-1.5">
            <h1 id="connect-title" className="text-title">
              {t('webConnection.connectTitle')}
            </h1>
            <p className="text-pretty text-text-muted">
              {reason === 'connecting'
                ? t('webConnection.connectBodyConnecting')
                : t('webConnection.connectBody')}
            </p>
          </div>
          <p className="inline-flex max-w-full items-center gap-2 rounded-full border border-border bg-surface-2 py-0.5 pr-2.5 pl-2 text-small text-text-muted">
            <span
              aria-hidden
              data-live={inFlight || undefined}
              className={`status-dot size-1.5 ${inFlight ? 'motion-status text-accent' : error ? 'text-danger' : 'text-text-muted'}`}
            />
            <span className="truncate">
              {t('webConnection.connectAddress', { address: hostOf(url || target) })}
            </span>
          </p>
        </div>
        {error ? (
          <p
            role="alert"
            className="anim-fade-in flex items-start gap-2.5 rounded-card border border-danger/30 bg-danger/[0.07] px-3 py-2.5"
          >
            <AlertTriangle
              className="mt-0.5 size-4 shrink-0 text-danger"
              aria-hidden
              data-status-icon
            />
            <span className="min-w-0">{error}</span>
          </p>
        ) : inFlight ? (
          <p role="status" className="flex items-center gap-2 text-small text-text-muted">
            <Loader2 className="motion-status size-3.5 animate-spin text-accent" aria-hidden />
            {t('webConnection.connectReaching', { host: hostOf(attemptUrl || url) })}
          </p>
        ) : null}
        <div className="flex flex-col gap-1.5">
          <label htmlFor="connect-token" className="text-label">
            {t('webConnection.connectToken')}
          </label>
          <Input
            id="connect-token"
            aria-describedby="connect-token-help"
            type="password"
            size="lg"
            icon={<KeyRound />}
            autoComplete="off"
            autoFocus
            value={token}
            onChange={(e) => setToken(e.target.value)}
            disabled={rateLimited}
          />
          <span id="connect-token-help" className="text-small text-text-muted">
            {tokenRequired
              ? t('webConnection.connectTokenStored')
              : `${t('webConnection.connectTokenStored')} ${t('webConnection.connectTokenOptional')}`}
          </span>
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="connect-url" className="text-label">
            {t('webConnection.connectUrl')}
          </label>
          <Input
            id="connect-url"
            aria-describedby="connect-url-help"
            type="url"
            size="lg"
            icon={<Globe />}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={rateLimited}
            className="font-mono text-[13px]"
          />
          <span id="connect-url-help" className="text-small text-text-muted">
            {t('webConnection.connectUrlHelp')}
          </span>
        </div>
        <Button
          type="submit"
          variant="primary"
          size="lg"
          disabled={rateLimited || (tokenRequired && !token.trim())}
          className="w-full"
        >
          {t('webConnection.connectSubmit')}
          <ArrowRight aria-hidden />
        </Button>
        <p className="border-t border-border pt-4 text-small text-pretty text-text-muted">
          {t('webConnection.connectWhere')}
        </p>
      </form>
    </main>
  )
}
