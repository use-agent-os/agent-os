import { Loader2, XCircle } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSettings'
import { cn } from '@/lib/utils'
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
    <div className="bg-atmosphere flex min-h-full items-center justify-center p-6 md:p-12">
      <section
        aria-live="polite"
        className="anim-enter flex w-full max-w-md flex-col items-center gap-3 rounded-card border border-border bg-surface-1 px-8 py-10 text-center shadow-card"
      >
        <span
          aria-hidden
          className={cn(
            'grid size-14 place-items-center rounded-overlay shadow-highlight',
            message ? 'bg-danger/10 text-danger' : 'bg-accent-tint text-accent',
          )}
        >
          {message ? (
            <XCircle className="size-7" data-status-icon />
          ) : (
            <Loader2 className="motion-status size-7 animate-spin" />
          )}
        </span>
        <h1 className="mt-1 text-section">
          {message ? t('webSettings.mcpOAuthFailed') : t('webSettings.mcpOAuthWorking')}
        </h1>
        {message ? (
          <p className="rounded-control bg-surface-inset px-3 py-2 font-mono text-small break-words text-text-muted shadow-inset">
            {message}
          </p>
        ) : null}
        {message ? (
          <Link
            to="/settings/mcp"
            replace
            className={cn(buttonVariants({ variant: 'secondary' }), 'mt-2')}
          >
            {t('webSettings.mcpOAuthBack')}
          </Link>
        ) : null}
      </section>
    </div>
  )
}
