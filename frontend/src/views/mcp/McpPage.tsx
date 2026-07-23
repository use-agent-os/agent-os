import './mcp.css'
import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence } from 'motion/react'
import {
  AlertTriangleIcon,
  ExternalLinkIcon,
  Globe2Icon,
  KeyRoundIcon,
  Link2Icon,
  NetworkIcon,
  PencilIcon,
  PlusIcon,
  PowerIcon,
  RefreshCwIcon,
  ServerIcon,
  ShieldCheckIcon,
  TerminalIcon,
  Trash2Icon,
  UnplugIcon,
  XIcon,
} from 'lucide-react'
import { toast } from 'sonner'
import { useBootstrap, useRpc } from '@/app/providers'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '@/components/ui/button'
import { MotionListItem } from '@/lib/motion'
import trustNetworkUrl from '@/assets/mcp-trust-network.webp'
import robinhoodSymbolUrl from '@/assets/robinhood-symbol.png'
import {
  ROBINHOOD_HELP_URL,
  ROBINHOOD_MCP_URL,
  createServerDraft,
  normalizeWorkspace,
  robinhoodPresentation,
  serverDetail,
  serverFromDraft,
  serverPresentation,
  transportLabel,
  validateServerDraft,
  type McpConfigResponse,
  type McpDraftErrors,
  type McpServerConfig,
  type McpServerDraft,
  type McpServerStatus,
  type McpStatusResponse,
  type McpTransport,
} from './logic'

interface McpConnectResponse {
  connected?: boolean
  authorizationRequired?: boolean
  authorizationUrl?: string
  tools?: string[]
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function ServerState({
  server,
  status,
  enabled,
  statusAvailable,
}: {
  server: McpServerConfig
  status?: McpServerStatus
  enabled: boolean
  statusAvailable: boolean
}) {
  const state = serverPresentation(server, status, enabled, statusAvailable)
  return (
    <span className={`mcp-state-chip is-${state.tone}`} title={state.detail}>
      <span aria-hidden="true" />
      {state.label}
    </span>
  )
}

function RuntimeSwitch({
  enabled,
  disabled,
  onToggle,
}: {
  enabled: boolean
  disabled: boolean
  onToggle: () => void
}) {
  return (
    <button
      type="button"
      className="mcp-runtime"
      role="switch"
      aria-checked={enabled}
      aria-label="Enable MCP runtime"
      disabled={disabled}
      onClick={onToggle}
    >
      <span className="mcp-runtime__icon" aria-hidden="true">
        <PowerIcon />
      </span>
      <span className="mcp-runtime__copy">
        <strong>MCP runtime</strong>
        <small>{enabled ? 'New connections enabled' : 'All connections paused'}</small>
      </span>
      <span className="mcp-runtime__track" aria-hidden="true">
        <span />
      </span>
    </button>
  )
}

function ServerEditor({
  draft,
  servers,
  busy,
  onChange,
  onClose,
  onSave,
}: {
  draft: McpServerDraft
  servers: McpServerConfig[]
  busy: boolean
  onChange: (draft: McpServerDraft) => void
  onClose: () => void
  onSave: (server: McpServerConfig) => void
}) {
  const [errors, setErrors] = useState<McpDraftErrors>({})
  const isHttp = draft.transport !== 'stdio'

  const update = <K extends keyof McpServerDraft>(key: K, value: McpServerDraft[K]) => {
    onChange({ ...draft, [key]: value })
    setErrors((current) => ({ ...current, [key]: undefined }))
  }

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    const nextErrors = validateServerDraft(draft, servers)
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length) return
    onSave(serverFromDraft(draft))
  }

  return (
    <ModalShell
      role="dialog"
      labelledBy="mcp-editor-title"
      describedBy="mcp-editor-description"
      onClose={busy ? () => {} : onClose}
      overlayClassName="mcp-modal__overlay"
      className="mcp-modal"
    >
      <form className="mcp-editor" onSubmit={submit} noValidate>
        <header className="mcp-editor__header">
          <div>
            <span className="mcp-editor__label">MCP connection</span>
            <h2 id="mcp-editor-title">{draft.originalName ? 'Edit server' : 'Add server'}</h2>
            <p id="mcp-editor-description">
              Save the connection, then discover its tools immediately.
            </p>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="Close dialog"
            disabled={busy}
            onClick={onClose}
          >
            <XIcon />
          </Button>
        </header>

        <div className="mcp-editor__body">
          <div className="mcp-form-grid">
            <label className="mcp-field">
              <span>Name</span>
              <input
                value={draft.name}
                aria-label="Name"
                maxLength={64}
                autoComplete="off"
                placeholder="my-mcp-server"
                aria-invalid={Boolean(errors.name)}
                aria-describedby="mcp-name-help mcp-name-error"
                onChange={(event) => update('name', event.target.value)}
              />
              <small id="mcp-name-help">Unique name used in logs and configuration.</small>
              <em id="mcp-name-error" role="alert">
                {errors.name}
              </em>
            </label>

            <label className="mcp-field">
              <span>Transport</span>
              <select
                value={draft.transport}
                aria-label="Transport"
                onChange={(event) => {
                  const transport = event.target.value as McpTransport
                  onChange({
                    ...draft,
                    transport,
                    oauth: transport === 'streamable_http' ? draft.oauth : false,
                  })
                }}
              >
                <option value="streamable_http">Streamable HTTP</option>
                <option value="sse">SSE (legacy)</option>
                <option value="stdio">Local process (stdio)</option>
              </select>
              <small>Use Streamable HTTP for new remote servers.</small>
              <em aria-hidden="true" />
            </label>
          </div>

          {isHttp ? (
            <>
              <label className="mcp-field">
                <span>Server URL</span>
                <input
                  type="url"
                  value={draft.url}
                  aria-label="Server URL"
                  autoComplete="url"
                  placeholder="https://example.com/mcp"
                  aria-invalid={Boolean(errors.url)}
                  aria-describedby="mcp-url-help mcp-url-error"
                  onChange={(event) => update('url', event.target.value)}
                />
                <small id="mcp-url-help">Use an absolute HTTP or HTTPS endpoint.</small>
                <em id="mcp-url-error" role="alert">
                  {errors.url}
                </em>
              </label>

              {draft.transport === 'streamable_http' ? (
                <label className="mcp-oauth-option">
                  <input
                    type="checkbox"
                    checked={draft.oauth}
                    aria-label="Authenticate with OAuth"
                    onChange={(event) => update('oauth', event.target.checked)}
                  />
                  <span className="mcp-oauth-option__icon" aria-hidden="true">
                    <KeyRoundIcon />
                  </span>
                  <span>
                    <strong>Authenticate with OAuth</strong>
                    <small>Store provider tokens privately in the AgentOS state directory.</small>
                  </span>
                </label>
              ) : null}

              <details className="mcp-advanced">
                <summary>Custom headers</summary>
                <label className="mcp-field">
                  <span>Headers (JSON)</span>
                  <textarea
                    rows={5}
                    value={draft.headers}
                    aria-label="Headers (JSON)"
                    spellCheck={false}
                    aria-invalid={Boolean(errors.headers)}
                    aria-describedby="mcp-headers-help mcp-headers-error"
                    onChange={(event) => update('headers', event.target.value)}
                  />
                  <small id="mcp-headers-help">
                    Prefer environment-backed configuration for long-lived secrets.
                  </small>
                  <em id="mcp-headers-error" role="alert">
                    {errors.headers}
                  </em>
                </label>
              </details>
            </>
          ) : (
            <div className="mcp-form-grid">
              <label className="mcp-field">
                <span>Command</span>
                <input
                  value={draft.command}
                  aria-label="Command"
                  autoComplete="off"
                  placeholder="uvx"
                  aria-invalid={Boolean(errors.command)}
                  aria-describedby="mcp-command-error"
                  onChange={(event) => update('command', event.target.value)}
                />
                <small>Executable used to start the local MCP server.</small>
                <em id="mcp-command-error" role="alert">
                  {errors.command}
                </em>
              </label>
              <label className="mcp-field">
                <span>Arguments</span>
                <input
                  value={draft.args}
                  aria-label="Arguments"
                  autoComplete="off"
                  placeholder="package-name --flag"
                  onChange={(event) => update('args', event.target.value)}
                />
                <small>Arguments are split on spaces. Use config for complex quoting.</small>
                <em aria-hidden="true" />
              </label>
            </div>
          )}

          <label className="mcp-field mcp-field--timeout">
            <span>Tool timeout</span>
            <span className="mcp-timeout-input">
              <input
                type="number"
                aria-label="Tool timeout"
                min={1}
                max={600}
                step={1}
                value={draft.timeout}
                aria-invalid={Boolean(errors.timeout)}
                aria-describedby="mcp-timeout-error"
                onChange={(event) => update('timeout', event.target.value)}
              />
              <span>seconds</span>
            </span>
            <em id="mcp-timeout-error" role="alert">
              {errors.timeout}
            </em>
          </label>
        </div>

        <footer className="mcp-editor__footer">
          <span role="status" aria-live="polite">
            {busy ? 'Saving connection...' : 'Configuration changes apply immediately.'}
          </span>
          <div>
            <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? 'Saving...' : 'Save and connect'}
            </Button>
          </div>
        </footer>
      </form>
    </ModalShell>
  )
}

function RemoveServerDialog({
  serverName,
  busy,
  onClose,
  onConfirm,
}: {
  serverName: string
  busy: boolean
  onClose: () => void
  onConfirm: () => void
}) {
  return (
    <ModalShell
      role="alertdialog"
      labelledBy="mcp-remove-title"
      describedBy="mcp-remove-description"
      onClose={busy ? () => {} : onClose}
      overlayClassName="mcp-modal__overlay"
      className="mcp-modal mcp-modal--confirm"
    >
      <div className="mcp-confirm__icon" aria-hidden="true">
        <Trash2Icon />
      </div>
      <h2 id="mcp-remove-title">Remove MCP server?</h2>
      <p id="mcp-remove-description">
        Remove <strong>{serverName}</strong> from AgentOS. Stored OAuth tokens will also be cleared.
      </p>
      <div className="mcp-confirm__actions">
        <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>
          Cancel
        </Button>
        <Button type="button" variant="destructive" disabled={busy} onClick={onConfirm}>
          {busy ? 'Removing...' : 'Remove server'}
        </Button>
      </div>
    </ModalShell>
  )
}

function OAuthCallback() {
  const rpc = useRpc()
  const location = useLocation()
  const navigate = useNavigate()
  const params = new URLSearchParams(location.search)
  const code = params.get('code')
  const oauthState = params.get('state')
  const providerError = params.get('error')
  const invalidMessage =
    providerError || !code || !oauthState
      ? params.get('error_description') ||
        providerError ||
        'The callback is missing its authorization code or state.'
      : null
  const [completionError, setCompletionError] = useState<string | null>(null)
  const state = invalidMessage || completionError ? 'error' : 'working'
  const message =
    invalidMessage || completionError || 'Exchanging the authorization code and loading tools.'

  useEffect(() => {
    document.title = 'MCP Authorization - AgentOS Control'
    if (invalidMessage || !code || !oauthState) return

    let cancelled = false
    void (async () => {
      try {
        await rpc.waitForConnection()
        await rpc.call('mcp.oauth.complete', { code, state: oauthState })
        if (cancelled) return
        toast.success('MCP authorization complete.')
        navigate('/mcp', { replace: true })
      } catch (error) {
        if (cancelled) return
        setCompletionError(errorMessage(error))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [code, invalidMessage, navigate, oauthState, rpc])

  return (
    <section className="mcp-callback" aria-live="polite">
      <span className={`mcp-callback__icon is-${state}`} aria-hidden="true">
        {state === 'working' ? <RefreshCwIcon /> : <XIcon />}
      </span>
      <h1>{state === 'working' ? 'Completing authorization' : 'Authorization not completed'}</h1>
      <p>{message}</p>
      {state === 'error' ? (
        <Button type="button" variant="outline" onClick={() => navigate('/mcp')}>
          Back to MCP servers
        </Button>
      ) : null}
    </section>
  )
}

export function McpPage() {
  const rpc = useRpc()
  const bootstrap = useBootstrap()
  const queryClient = useQueryClient()
  const location = useLocation()
  const isCallback = location.pathname.toLowerCase().endsWith('/mcp/oauth/callback')
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [editor, setEditor] = useState<McpServerDraft | null>(null)
  const [removeTarget, setRemoveTarget] = useState<string | null>(null)

  useEffect(() => {
    if (!isCallback) document.title = 'MCP Servers - AgentOS Control'
  }, [isCallback])

  const workspaceQuery = useQuery({
    queryKey: ['mcp', 'workspace'],
    enabled: !isCallback,
    retry: false,
    queryFn: async () => {
      await rpc.waitForConnection()
      const [config, statusResult] = await Promise.all([
        rpc.call<McpConfigResponse>('config.get'),
        rpc.call<McpStatusResponse>('mcp.status').then(
          (status) => ({ status, error: null }),
          (error: unknown) => ({
            status: {} as McpStatusResponse,
            error: errorMessage(error),
          }),
        ),
      ])
      return {
        workspace: normalizeWorkspace(config, statusResult.status),
        statusError: statusResult.error,
      }
    },
  })

  if (isCallback) return <OAuthCallback />

  const workspace = workspaceQuery.data?.workspace ?? normalizeWorkspace({}, {})
  const statusError = workspaceQuery.data?.statusError ?? null
  const statusAvailable = !statusError
  const robinhood = robinhoodPresentation(
    workspace.servers,
    workspace.statusByName,
    workspace.enabled,
    statusAvailable,
  )
  const robinhoodServer = workspace.servers.find((server) => server.url === ROBINHOOD_MCP_URL)
  const connectedCount = Object.values(workspace.statusByName).filter(
    (status) => status.connected,
  ).length
  const toolCount = Object.values(workspace.statusByName).reduce(
    (total, status) => total + (status.tools?.length ?? 0),
    0,
  )

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ['mcp'] })
  }

  const patchServers = async (servers: McpServerConfig[], enabled = workspace.enabled) => {
    await rpc.call('config.patch', {
      patches: { 'mcp.enabled': enabled, 'mcp.servers': servers },
    })
  }

  const authorize = async (name: string) => {
    const basePath = bootstrap.base_path.replace(/\/$/, '')
    const result = await rpc.call<McpConnectResponse>('mcp.oauth.start', {
      name,
      redirectUri: `${window.location.origin}${basePath}/mcp/oauth/callback`,
    })
    if (result.connected) return
    if (!result.authorizationUrl) {
      throw new Error('The MCP server did not provide an authorization URL.')
    }
    window.location.assign(result.authorizationUrl)
  }

  const connect = async (name: string) => {
    setBusyAction(`connect:${name}`)
    try {
      const result = await rpc.call<McpConnectResponse>('mcp.connect', { name })
      if (result.authorizationRequired) await authorize(name)
      else toast.success(`${name} connected.`)
      await refresh()
    } catch (error) {
      toast.error(errorMessage(error))
      await refresh()
    } finally {
      setBusyAction(null)
    }
  }

  const disconnect = async (name: string) => {
    setBusyAction(`disconnect:${name}`)
    try {
      await rpc.call('mcp.disconnect', { name })
      toast.success(`${name} disconnected.`)
      await refresh()
    } catch (error) {
      toast.error(errorMessage(error))
    } finally {
      setBusyAction(null)
    }
  }

  const toggleRuntime = async () => {
    const enabled = !workspace.enabled
    setBusyAction('runtime')
    try {
      await patchServers(workspace.servers, enabled)
      if (!enabled) {
        await Promise.allSettled(
          workspace.servers.map((server) => rpc.call('mcp.disconnect', { name: server.name })),
        )
      }
      toast.success(enabled ? 'MCP runtime enabled.' : 'MCP runtime paused.')
      await refresh()
    } catch (error) {
      toast.error(errorMessage(error))
    } finally {
      setBusyAction(null)
    }
  }

  const saveServer = async (server: McpServerConfig) => {
    if (!editor) return
    const renamedFrom =
      editor.originalName && editor.originalName !== server.name ? editor.originalName : null
    const servers = [...workspace.servers]
    const index = servers.findIndex((entry) => entry.name === editor.originalName)
    if (index >= 0) servers[index] = server
    else servers.push(server)
    setBusyAction('editor')
    try {
      if (renamedFrom) {
        try {
          await rpc.call('mcp.oauth.clear', { name: renamedFrom })
        } catch {
          await rpc.call('mcp.disconnect', { name: renamedFrom })
        }
      }
      await patchServers(servers, true)
      setEditor(null)
      await refresh()
      await connect(server.name)
    } catch (error) {
      toast.error(errorMessage(error))
      setBusyAction(null)
    }
  }

  const removeServer = async () => {
    if (!removeTarget) return
    setBusyAction(`remove:${removeTarget}`)
    try {
      try {
        await rpc.call('mcp.oauth.clear', { name: removeTarget })
      } catch {
        await rpc.call('mcp.disconnect', { name: removeTarget })
      }
      await patchServers(workspace.servers.filter((server) => server.name !== removeTarget))
      toast.success(`${removeTarget} removed.`)
      setRemoveTarget(null)
      await refresh()
    } catch (error) {
      toast.error(errorMessage(error))
    } finally {
      setBusyAction(null)
    }
  }

  const editServer = (server: McpServerConfig) => {
    setEditor(createServerDraft({ ...server, originalName: server.name }))
  }

  const openRobinhood = () => {
    if (robinhoodServer) editServer(robinhoodServer)
    else {
      setEditor(
        createServerDraft({
          name: 'robinhood-trading',
          url: ROBINHOOD_MCP_URL,
          oauth: true,
        }),
      )
    }
  }

  if (workspaceQuery.isLoading) {
    return (
      <section className="mcp-stage" aria-busy="true" aria-label="Loading MCP servers">
        <div className="mcp-skeleton mcp-skeleton--header" />
        <div className="mcp-skeleton mcp-skeleton--feature" />
        <div className="mcp-skeleton mcp-skeleton--row" />
      </section>
    )
  }

  if (workspaceQuery.isError) {
    return (
      <section className="mcp-stage">
        <div className="mcp-load-error" role="alert">
          <span aria-hidden="true">
            <AlertTriangleIcon />
          </span>
          <h1>MCP configuration unavailable</h1>
          <p>{errorMessage(workspaceQuery.error)}</p>
          <Button type="button" variant="outline" onClick={() => void workspaceQuery.refetch()}>
            <RefreshCwIcon />
            Retry
          </Button>
        </div>
      </section>
    )
  }

  return (
    <section className="mcp-stage">
      <header className="mcp-stage__header">
        <div className="mcp-stage__title-block">
          <div className="t-label">Connections</div>
          <h1 className="t-display">MCP Servers</h1>
          <p>Control which external tools your agents can discover and use.</p>
        </div>
        <div className="mcp-stage__actions">
          <Button
            type="button"
            variant="outline"
            disabled={workspaceQuery.isFetching || Boolean(busyAction)}
            onClick={() => void refresh()}
          >
            <RefreshCwIcon className={workspaceQuery.isFetching ? 'mcp-spin' : undefined} />
            Refresh
          </Button>
          <RuntimeSwitch
            enabled={workspace.enabled}
            disabled={busyAction === 'runtime'}
            onToggle={() => void toggleRuntime()}
          />
        </div>
      </header>

      {statusError ? (
        <div className="mcp-status-warning" role="status">
          <span className="mcp-status-warning__icon" aria-hidden="true">
            <AlertTriangleIcon />
          </span>
          <div>
            <strong>Live MCP status is unavailable.</strong>
            <p>
              Your configuration is still available. Restart the gateway if this persists, then
              retry the status check.
            </p>
          </div>
          <Button
            type="button"
            variant="outline"
            disabled={workspaceQuery.isFetching}
            title={statusError}
            onClick={() => void refresh()}
          >
            <RefreshCwIcon className={workspaceQuery.isFetching ? 'mcp-spin' : undefined} />
            Retry status
          </Button>
        </div>
      ) : null}

      <div className="mcp-summary" aria-label="MCP summary">
        <div>
          <span>Configured</span>
          <strong>{workspace.servers.length}</strong>
        </div>
        <div>
          <span>Connected</span>
          <strong>{statusAvailable ? connectedCount : '—'}</strong>
        </div>
        <div>
          <span>Live tools</span>
          <strong>{statusAvailable ? toolCount : '—'}</strong>
        </div>
        <div
          className={`mcp-summary__runtime is-${
            statusAvailable ? (workspace.enabled ? 'live' : 'paused') : 'unavailable'
          }`}
        >
          <span>Runtime posture</span>
          <strong>
            {statusAvailable
              ? workspace.enabled
                ? 'Accepting connections'
                : 'Paused'
              : 'Live status unavailable'}
          </strong>
        </div>
      </div>

      <article className={`mcp-partner is-${robinhood.tone}`} aria-label="Robinhood MCP">
        <img className="mcp-partner__network" src={trustNetworkUrl} alt="" aria-hidden="true" />
        <div className="mcp-partner__content">
          <div className="mcp-partner__brand">
            <img src={robinhoodSymbolUrl} alt="Robinhood logo" width="48" height="48" />
            <div>
              <span>Featured integration</span>
              <h2>
                Robinhood <small>for AgentOS</small>
              </h2>
            </div>
          </div>
          <span className={`mcp-partner__state is-${robinhood.tone}`}>
            <span aria-hidden="true" />
            {robinhood.label}
          </span>
          <h3>A controlled path from your agent to the market.</h3>
          <p>
            Connect a dedicated Agentic Trading account with secure authorization and live tool
            discovery.
          </p>
          <div className="mcp-partner__capabilities" aria-label="Connection capabilities">
            <span>
              <ShieldCheckIcon /> OAuth + PKCE
            </span>
            <span>
              <Globe2Icon /> Streamable HTTP
            </span>
            <span>
              <NetworkIcon /> Live registration
            </span>
          </div>
          <div className="mcp-partner__actions">
            <Button type="button" onClick={openRobinhood}>
              <Link2Icon />
              {robinhood.action}
            </Button>
            <Button asChild variant="ghost">
              <a href={ROBINHOOD_HELP_URL} target="_blank" rel="noopener noreferrer">
                Setup guide <ExternalLinkIcon />
              </a>
            </Button>
          </div>
        </div>

        <div className="mcp-partner__connection">
          <div className="mcp-partner__connection-head">
            <span>Connection architecture</span>
            <strong>{robinhood.detail}</strong>
          </div>
          <div className="mcp-flow" aria-label="AgentOS connects securely to Robinhood MCP">
            <div className="mcp-flow__node">
              <span aria-hidden="true">
                <NetworkIcon />
              </span>
              <small>Local gateway</small>
              <strong>AgentOS</strong>
            </div>
            <div className="mcp-flow__rail" aria-hidden="true">
              <span>OAuth</span>
            </div>
            <div className="mcp-flow__node">
              <img src={robinhoodSymbolUrl} alt="" width="32" height="32" />
              <small>Remote server</small>
              <strong>Robinhood MCP</strong>
            </div>
          </div>
          <dl className="mcp-partner__specs">
            <div>
              <dt>Endpoint</dt>
              <dd title={ROBINHOOD_MCP_URL}>{ROBINHOOD_MCP_URL}</dd>
            </div>
            <div>
              <dt>Authorization</dt>
              <dd>OAuth with PKCE</dd>
            </div>
            <div>
              <dt>Tool loading</dt>
              <dd>{robinhood.tools}</dd>
            </div>
          </dl>
        </div>
        <div className="mcp-partner__notice" role="note">
          <AlertTriangleIcon aria-hidden="true" />
          <span>
            <strong>Human-controlled by design.</strong> You approve the account link and remain
            responsible for every order. Agentic trading involves significant risk.
          </span>
        </div>
      </article>

      <div className="mcp-security-note" role="note">
        <ShieldCheckIcon aria-hidden="true" />
        <span>
          <strong>Review every MCP permission.</strong> Connected servers can expose private data
          and tools that take actions on your behalf.
        </span>
      </div>

      <section className="mcp-servers" aria-labelledby="mcp-servers-title">
        <header className="mcp-servers__header">
          <div>
            <h2 id="mcp-servers-title">Your servers</h2>
            <p>
              {workspace.servers.length
                ? `${workspace.servers.length} configured connection${workspace.servers.length === 1 ? '' : 's'}`
                : 'No custom servers configured yet'}
            </p>
          </div>
          <Button type="button" variant="outline" onClick={() => setEditor(createServerDraft())}>
            <PlusIcon />
            Add server
          </Button>
        </header>

        {workspace.servers.length ? (
          <div className="mcp-server-list">
            <AnimatePresence initial={false}>
              {workspace.servers.map((server) => {
                const status = workspace.statusByName[server.name]
                const presentation = serverPresentation(
                  server,
                  status,
                  workspace.enabled,
                  statusAvailable,
                )
                const actionBusy = busyAction?.endsWith(`:${server.name}`)
                return (
                  <MotionListItem className="mcp-server-row" key={server.name}>
                    <span className="mcp-server-row__icon" aria-hidden="true">
                      {server.transport === 'stdio' ? <TerminalIcon /> : <ServerIcon />}
                    </span>
                    <div className="mcp-server-row__main">
                      <div className="mcp-server-row__title">
                        <h3>{server.name}</h3>
                        <ServerState
                          server={server}
                          status={status}
                          enabled={workspace.enabled}
                          statusAvailable={statusAvailable}
                        />
                      </div>
                      <div className="mcp-server-row__meta">
                        <span>{transportLabel(server.transport)}</span>
                        {server.oauth ? <span>OAuth</span> : null}
                        {presentation.toolCount ? (
                          <span>
                            {presentation.toolCount} tool{presentation.toolCount === 1 ? '' : 's'}
                          </span>
                        ) : null}
                      </div>
                      <code title={serverDetail(server)}>{serverDetail(server)}</code>
                    </div>
                    <div className="mcp-server-row__actions">
                      {statusAvailable && workspace.enabled && !status?.connected ? (
                        <Button
                          type="button"
                          size="sm"
                          disabled={actionBusy}
                          onClick={() => void connect(server.name)}
                        >
                          <Link2Icon />
                          {actionBusy
                            ? 'Connecting...'
                            : server.oauth && !status?.authenticated
                              ? 'Authorize'
                              : 'Connect'}
                        </Button>
                      ) : null}
                      {status?.connected ? (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={actionBusy}
                          onClick={() => void disconnect(server.name)}
                        >
                          <UnplugIcon />
                          {actionBusy ? 'Disconnecting...' : 'Disconnect'}
                        </Button>
                      ) : null}
                      <Button
                        type="button"
                        size="icon-sm"
                        variant="ghost"
                        aria-label={`Edit ${server.name}`}
                        title={`Edit ${server.name}`}
                        onClick={() => editServer(server)}
                      >
                        <PencilIcon />
                      </Button>
                      <Button
                        type="button"
                        size="icon-sm"
                        variant="ghost"
                        className="mcp-server-row__remove"
                        aria-label={`Remove ${server.name}`}
                        title={`Remove ${server.name}`}
                        onClick={() => setRemoveTarget(server.name)}
                      >
                        <Trash2Icon />
                      </Button>
                    </div>
                  </MotionListItem>
                )
              })}
            </AnimatePresence>
          </div>
        ) : (
          <div className="mcp-empty">
            <span aria-hidden="true">
              <NetworkIcon />
            </span>
            <h3>No MCP servers</h3>
            <p>Add a server URL or configure the Robinhood connection above.</p>
            <Button type="button" variant="outline" onClick={() => setEditor(createServerDraft())}>
              <PlusIcon />
              Add first server
            </Button>
          </div>
        )}
      </section>

      <AnimatePresence>
        {editor ? (
          <ServerEditor
            draft={editor}
            servers={workspace.servers}
            busy={busyAction === 'editor'}
            onChange={setEditor}
            onClose={() => setEditor(null)}
            onSave={(server) => void saveServer(server)}
          />
        ) : null}
      </AnimatePresence>

      <AnimatePresence>
        {removeTarget ? (
          <RemoveServerDialog
            serverName={removeTarget}
            busy={busyAction === `remove:${removeTarget}`}
            onClose={() => setRemoveTarget(null)}
            onConfirm={() => void removeServer()}
          />
        ) : null}
      </AnimatePresence>
    </section>
  )
}
