import { Loader2, XCircle } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSettings'
import { buttonVariants } from '@/web/components/ui/button'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** Outcome handed to Settings › MCP through navigation state. */
export interface McpOAuthOutcome {
  mcpOAuth: { ok: true } | { ok: false; message: string }
}

/**
 * `<base>/mcp/oauth/callback?code=…&state=…` — finish MCP OAuth at the exact
 * path the authorization server returns to, then show Settings › MCP with the
 * outcome (shell spec "MCP OAuth callback path is preserved").
 */
export function McpOAuthCallback() {
  const rpc = useRpc()
  const navigate = useNavigate()
  const { search } = useLocation()
  const params = new URLSearchParams(search)
  const code = params.get('code')
  const oauthState = params.get('state')
  const providerError = params.get('error')
  const invalid =
    providerError || !code || !oauthState
      ? params.get('error_description') || providerError || t('webSettings.mcpOAuthMissing')
      : null
  const [failure, setFailure] = useState<string | null>(null)

  useEffect(() => {
    if (invalid || !code || !oauthState) return
    let cancelled = false
    void (async () => {
      try {
        await rpc.waitForConnection()
        await rpc.call('mcp.oauth.complete', { code, state: oauthState })
        if (cancelled) return
        const state: McpOAuthOutcome = { mcpOAuth: { ok: true } }
        navigate('/settings/mcp', { replace: true, state })
      } catch (error) {
        if (!cancelled) setFailure(errorMessage(error))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [code, invalid, navigate, oauthState, rpc])

  const message = invalid ?? failure
  return (
    <section
      aria-live="polite"
      className="anim-enter mx-auto my-12 flex max-w-md flex-col items-center gap-3 rounded-card border border-border bg-surface-1 p-10 text-center shadow-card"
    >
      {message ? (
        <XCircle className="size-8 text-danger" aria-hidden data-status-icon />
      ) : (
        <Loader2 className="motion-status size-8 animate-spin text-accent" aria-hidden />
      )}
      <h1 className="text-section">
        {message ? t('webSettings.mcpOAuthFailed') : t('webSettings.mcpOAuthWorking')}
      </h1>
      {message ? <p className="text-text-muted">{message}</p> : null}
      {message ? (
        <Link to="/settings/mcp" replace className={buttonVariants()}>
          {t('webSettings.mcpOAuthBack')}
        </Link>
      ) : null}
    </section>
  )
}
