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
import { t, tPlural } from '@/i18n'
import '@/i18n/en/mcp'
import { MotionListItem } from '@/lib/motion'
import trustNetworkUrl from '@/assets/mcp-trust-network.webp'
import robinhoodSymbolUrl from '@/assets/robinhood-symbol.png'
import baseSymbolUrl from '@/assets/base-symbol.png'
import {
  MCP_PARTNERS,
  createServerDraft,
  normalizeWorkspace,
  partnerPresentation,
  serverDetail,
  serverFromDraft,
  serverPresentation,
  transportLabel,
  validateServerDraft,
  type McpConfigResponse,
  type McpDraftErrors,
  type McpPartner,
  type McpServerConfig,
  type McpServerDraft,
  type McpServerStatus,
  type McpStatusResponse,
  type McpTransport,
  type PartnerPresentation,
} from './logic'

const PARTNER_LOGOS: Record<McpPartner['id'], string> = {
  robinhood: robinhoodSymbolUrl,
  base: baseSymbolUrl,
}

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
      aria-label={t('mcp.runtimeAria')}
      disabled={disabled}
      onClick={onToggle}
    >
      <span className="mcp-runtime__icon" aria-hidden="true">
        <PowerIcon />
      </span>
      <span className="mcp-runtime__copy">
        <strong>{t('mcp.runtimeTitle')}</strong>
        <small>{enabled ? t('mcp.runtimeOn') : t('mcp.runtimeOff')}</small>
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
            <span className="mcp-editor__label">{t('mcp.editorEyebrow')}</span>
            <h2 id="mcp-editor-title">
              {draft.originalName ? t('mcp.editorEditTitle') : t('mcp.editorAddTitle')}
            </h2>
            <p id="mcp-editor-description">{t('mcp.editorDescription')}</p>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={t('mcp.editorClose')}
            disabled={busy}
            onClick={onClose}
          >
            <XIcon />
          </Button>
        </header>

        <div className="mcp-editor__body">
          <div className="mcp-form-grid">
            <label className="mcp-field">
              <span>{t('mcp.fieldName')}</span>
              <input
                value={draft.name}
                aria-label={t('mcp.fieldName')}
                maxLength={64}
                autoComplete="off"
                placeholder={t('mcp.fieldNamePlaceholder')}
                aria-invalid={Boolean(errors.name)}
                aria-describedby="mcp-name-help mcp-name-error"
                onChange={(event) => update('name', event.target.value)}
              />
              <small id="mcp-name-help">{t('mcp.fieldNameHelp')}</small>
              <em id="mcp-name-error" role="alert">
                {errors.name}
              </em>
            </label>

            <label className="mcp-field">
              <span>{t('mcp.fieldTransport')}</span>
              <select
                value={draft.transport}
                aria-label={t('mcp.fieldTransport')}
                onChange={(event) => {
                  const transport = event.target.value as McpTransport
                  onChange({
                    ...draft,
                    transport,
                    oauth: transport === 'streamable_http' ? draft.oauth : false,
                  })
                }}
              >
                <option value="streamable_http">{t('mcp.transportStreamableHttpOption')}</option>
                <option value="sse">{t('mcp.transportSseOption')}</option>
                <option value="stdio">{t('mcp.transportStdioOption')}</option>
              </select>
              <small>{t('mcp.fieldTransportHelp')}</small>
              <em aria-hidden="true" />
            </label>
          </div>

          {isHttp ? (
            <>
              <label className="mcp-field">
                <span>{t('mcp.fieldUrl')}</span>
                <input
                  type="url"
                  value={draft.url}
                  aria-label={t('mcp.fieldUrl')}
                  autoComplete="url"
                  placeholder={t('mcp.fieldUrlPlaceholder')}
                  aria-invalid={Boolean(errors.url)}
                  aria-describedby="mcp-url-help mcp-url-error"
                  onChange={(event) => update('url', event.target.value)}
                />
                <small id="mcp-url-help">{t('mcp.fieldUrlHelp')}</small>
                <em id="mcp-url-error" role="alert">
                  {errors.url}
                </em>
              </label>

              {draft.transport === 'streamable_http' ? (
                <label className="mcp-oauth-option">
                  <input
                    type="checkbox"
                    checked={draft.oauth}
                    aria-label={t('mcp.oauthAria')}
                    onChange={(event) => update('oauth', event.target.checked)}
                  />
                  <span className="mcp-oauth-option__icon" aria-hidden="true">
                    <KeyRoundIcon />
                  </span>
                  <span>
                    <strong>{t('mcp.oauthTitle')}</strong>
                    <small>{t('mcp.oauthHelp')}</small>
                  </span>
                </label>
              ) : null}

              <details className="mcp-advanced">
                <summary>{t('mcp.headersSummary')}</summary>
                <label className="mcp-field">
                  <span>{t('mcp.fieldHeaders')}</span>
                  <textarea
                    rows={5}
                    value={draft.headers}
                    aria-label={t('mcp.fieldHeaders')}
                    spellCheck={false}
                    aria-invalid={Boolean(errors.headers)}
                    aria-describedby="mcp-headers-help mcp-headers-error"
                    onChange={(event) => update('headers', event.target.value)}
                  />
                  <small id="mcp-headers-help">{t('mcp.fieldHeadersHelp')}</small>
                  <em id="mcp-headers-error" role="alert">
                    {errors.headers}
                  </em>
                </label>
              </details>
            </>
          ) : (
            <div className="mcp-form-grid">
              <label className="mcp-field">
                <span>{t('mcp.fieldCommand')}</span>
                <input
                  value={draft.command}
                  aria-label={t('mcp.fieldCommand')}
                  autoComplete="off"
                  placeholder={t('mcp.fieldCommandPlaceholder')}
                  aria-invalid={Boolean(errors.command)}
                  aria-describedby="mcp-command-error"
                  onChange={(event) => update('command', event.target.value)}
                />
                <small>{t('mcp.fieldCommandHelp')}</small>
                <em id="mcp-command-error" role="alert">
                  {errors.command}
                </em>
              </label>
              <label className="mcp-field">
                <span>{t('mcp.fieldArgs')}</span>
                <input
                  value={draft.args}
                  aria-label={t('mcp.fieldArgs')}
                  autoComplete="off"
                  placeholder={t('mcp.fieldArgsPlaceholder')}
                  onChange={(event) => update('args', event.target.value)}
                />
                <small>{t('mcp.fieldArgsHelp')}</small>
                <em aria-hidden="true" />
              </label>
            </div>
          )}

          <label className="mcp-field mcp-field--timeout">
            <span>{t('mcp.fieldTimeout')}</span>
            <span className="mcp-timeout-input">
              <input
                type="number"
                aria-label={t('mcp.fieldTimeout')}
                min={1}
                max={600}
                step={1}
                value={draft.timeout}
                aria-invalid={Boolean(errors.timeout)}
                aria-describedby="mcp-timeout-error"
                onChange={(event) => update('timeout', event.target.value)}
              />
              <span>{t('mcp.fieldTimeoutUnit')}</span>
            </span>
            <em id="mcp-timeout-error" role="alert">
              {errors.timeout}
            </em>
          </label>
        </div>

        <footer className="mcp-editor__footer">
          <span role="status" aria-live="polite">
            {busy ? t('mcp.editorSaving') : t('mcp.editorImmediate')}
          </span>
          <div>
            <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? t('mcp.editorSaveBusy') : t('mcp.editorSave')}
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
      <h2 id="mcp-remove-title">{t('mcp.removeTitle')}</h2>
      <p id="mcp-remove-description">
        {t('mcp.removeBodyLead')} <strong>{serverName}</strong> {t('mcp.removeBodyTail')}
      </p>
      <div className="mcp-confirm__actions">
        <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>
          {t('common.cancel')}
        </Button>
        <Button type="button" variant="destructive" disabled={busy} onClick={onConfirm}>
          {busy ? t('mcp.removeBusy') : t('mcp.removeConfirm')}
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
  const message = invalidMessage || completionError || t('mcp.callbackMessage')

  useEffect(() => {
    document.title = t('mcp.callbackDocumentTitle')
    if (invalidMessage || !code || !oauthState) return

    let cancelled = false
    void (async () => {
      try {
        await rpc.waitForConnection()
        await rpc.call('mcp.oauth.complete', { code, state: oauthState })
        if (cancelled) return
        toast.success(t('mcp.toastAuthorized'))
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
      <h1>{state === 'working' ? t('mcp.callbackWorking') : t('mcp.callbackFailed')}</h1>
      <p>{message}</p>
      {state === 'error' ? (
        <Button type="button" variant="outline" onClick={() => navigate('/mcp')}>
          {t('mcp.callbackBack')}
        </Button>
      ) : null}
    </section>
  )
}

interface PartnerCardCopy {
  landmark: string
  logoAlt: string
  headline: string
  body: string
  flowLandmark: string
  flowRemoteName: string
  noticeLead: string
  noticeBody: string
}

function partnerCardCopy(partner: McpPartner): PartnerCardCopy {
  if (partner.id === 'base') {
    return {
      landmark: t('mcp.basePartnerLandmark'),
      logoAlt: t('mcp.basePartnerLogoAlt'),
      headline: t('mcp.basePartnerHeadline'),
      body: t('mcp.basePartnerBody'),
      flowLandmark: t('mcp.basePartnerFlowLandmark'),
      flowRemoteName: t('mcp.basePartnerFlowRemoteName'),
      noticeLead: t('mcp.basePartnerNoticeLead'),
      noticeBody: t('mcp.basePartnerNoticeBody'),
    }
  }
  return {
    landmark: t('mcp.partnerLandmark'),
    logoAlt: t('mcp.partnerLogoAlt'),
    headline: t('mcp.partnerHeadline'),
    body: t('mcp.partnerBody'),
    flowLandmark: t('mcp.partnerFlowLandmark'),
    flowRemoteName: t('mcp.partnerFlowRemoteName'),
    noticeLead: t('mcp.partnerNoticeLead'),
    noticeBody: t('mcp.partnerNoticeBody'),
  }
}

function PartnerCard({
  partner,
  presentation,
  onOpen,
}: {
  partner: McpPartner
  presentation: PartnerPresentation
  onOpen: () => void
}) {
  const logoUrl = PARTNER_LOGOS[partner.id]
  const copy = partnerCardCopy(partner)
  return (
    <article className={`mcp-partner is-${presentation.tone}`} aria-label={copy.landmark}>
      <img className="mcp-partner__network" src={trustNetworkUrl} alt="" aria-hidden="true" />
      <div className="mcp-partner__content">
        <div className="mcp-partner__brand">
          <img src={logoUrl} alt={copy.logoAlt} width="48" height="48" />
          <div>
            <span>{t('mcp.partnerEyebrow')}</span>
            <h2>
              {partner.name} <small>{t('mcp.partnerForAgentos')}</small>
            </h2>
          </div>
        </div>
        <span className={`mcp-partner__state is-${presentation.tone}`}>
          <span aria-hidden="true" />
          {presentation.label}
        </span>
        <h3>{copy.headline}</h3>
        <p>{copy.body}</p>
        <div className="mcp-partner__capabilities" aria-label={t('mcp.partnerCapabilities')}>
          <span>
            <ShieldCheckIcon /> {t('mcp.partnerCapOauth')}
          </span>
          <span>
            <Globe2Icon /> {t('mcp.partnerCapTransport')}
          </span>
          <span>
            <NetworkIcon /> {t('mcp.partnerCapRegistration')}
          </span>
        </div>
        <div className="mcp-partner__actions">
          <Button type="button" onClick={onOpen}>
            <Link2Icon />
            {presentation.action}
          </Button>
          <Button asChild variant="ghost">
            <a href={partner.helpUrl} target="_blank" rel="noopener noreferrer">
              {t('mcp.partnerSetupGuide')} <ExternalLinkIcon />
            </a>
          </Button>
        </div>
      </div>

      <div className="mcp-partner__connection">
        <div className="mcp-partner__connection-head">
          <span>{t('mcp.partnerArchitecture')}</span>
          <strong>{presentation.detail}</strong>
        </div>
        <div className="mcp-flow" aria-label={copy.flowLandmark}>
          <div className="mcp-flow__node">
            <span aria-hidden="true">
              <NetworkIcon />
            </span>
            <small>{t('mcp.partnerFlowLocal')}</small>
            <strong>{'AgentOS'}</strong>
          </div>
          <div className="mcp-flow__rail" aria-hidden="true">
            <span>{t('mcp.partnerFlowOauth')}</span>
          </div>
          <div className="mcp-flow__node">
            <img src={logoUrl} alt="" width="32" height="32" />
            <small>{t('mcp.partnerFlowRemote')}</small>
            <strong>{copy.flowRemoteName}</strong>
          </div>
        </div>
        <dl className="mcp-partner__specs">
          <div>
            <dt>{t('mcp.partnerSpecEndpoint')}</dt>
            <dd title={partner.url}>{partner.url}</dd>
          </div>
          <div>
            <dt>{t('mcp.partnerSpecAuthorization')}</dt>
            <dd>{t('mcp.partnerSpecAuthorizationValue')}</dd>
          </div>
          <div>
            <dt>{t('mcp.partnerSpecTools')}</dt>
            <dd>{presentation.tools}</dd>
          </div>
        </dl>
      </div>
      <div className="mcp-partner__notice" role="note">
        <AlertTriangleIcon aria-hidden="true" />
        <span>
          <strong>{copy.noticeLead}</strong> {copy.noticeBody}
        </span>
      </div>
    </article>
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
    if (!isCallback) document.title = t('mcp.documentTitle')
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
  const partnerStates = MCP_PARTNERS.map((partner) => ({
    partner,
    presentation: partnerPresentation(
      partner,
      workspace.servers,
      workspace.statusByName,
      workspace.enabled,
      statusAvailable,
    ),
    server: workspace.servers.find((server) => server.url === partner.url),
  }))
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
      else toast.success(t('mcp.toastConnected', { name }))
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
      toast.success(t('mcp.toastDisconnected', { name }))
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
      toast.success(enabled ? t('mcp.toastRuntimeEnabled') : t('mcp.toastRuntimePaused'))
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
      toast.success(t('mcp.toastRemoved', { name: removeTarget }))
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

  const openPartner = (partner: McpPartner, existing: McpServerConfig | undefined) => {
    if (existing) editServer(existing)
    else {
      setEditor(
        createServerDraft({
          name: partner.serverName,
          url: partner.url,
          oauth: true,
        }),
      )
    }
  }

  if (workspaceQuery.isLoading) {
    return (
      <section className="mcp-stage" aria-busy="true" aria-label={t('mcp.loadingLabel')}>
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
          <h1>{t('mcp.loadErrorTitle')}</h1>
          <p>{errorMessage(workspaceQuery.error)}</p>
          <Button type="button" variant="outline" onClick={() => void workspaceQuery.refetch()}>
            <RefreshCwIcon />
            {t('common.retry')}
          </Button>
        </div>
      </section>
    )
  }

  return (
    <section className="mcp-stage">
      <header className="mcp-stage__header">
        <div className="mcp-stage__title-block">
          <div className="t-label">{t('mcp.eyebrow')}</div>
          <h1 className="t-display">{t('mcp.title')}</h1>
          <p>{t('mcp.subtitle')}</p>
        </div>
        <div className="mcp-stage__actions">
          <Button
            type="button"
            variant="outline"
            disabled={workspaceQuery.isFetching || Boolean(busyAction)}
            onClick={() => void refresh()}
          >
            <RefreshCwIcon className={workspaceQuery.isFetching ? 'mcp-spin' : undefined} />
            {t('mcp.refresh')}
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
            <strong>{t('mcp.statusWarningTitle')}</strong>
            <p>{t('mcp.statusWarningBody')}</p>
          </div>
          <Button
            type="button"
            variant="outline"
            disabled={workspaceQuery.isFetching}
            title={statusError}
            onClick={() => void refresh()}
          >
            <RefreshCwIcon className={workspaceQuery.isFetching ? 'mcp-spin' : undefined} />
            {t('mcp.statusRetry')}
          </Button>
        </div>
      ) : null}

      <div className="mcp-summary" aria-label={t('mcp.summaryLandmark')}>
        <div>
          <span>{t('mcp.summaryConfigured')}</span>
          <strong>{workspace.servers.length}</strong>
        </div>
        <div>
          <span>{t('mcp.summaryConnected')}</span>
          <strong>{statusAvailable ? connectedCount : t('common.dash')}</strong>
        </div>
        <div>
          <span>{t('mcp.summaryLiveTools')}</span>
          <strong>{statusAvailable ? toolCount : t('common.dash')}</strong>
        </div>
        <div
          className={`mcp-summary__runtime is-${
            statusAvailable ? (workspace.enabled ? 'live' : 'paused') : 'unavailable'
          }`}
        >
          <span>{t('mcp.summaryRuntime')}</span>
          <strong>
            {statusAvailable
              ? workspace.enabled
                ? t('mcp.summaryAccepting')
                : t('mcp.summaryPaused')
              : t('mcp.summaryUnavailable')}
          </strong>
        </div>
      </div>

      {partnerStates.map(({ partner, presentation, server }) => (
        <PartnerCard
          key={partner.id}
          partner={partner}
          presentation={presentation}
          onOpen={() => openPartner(partner, server)}
        />
      ))}

      <div className="mcp-security-note" role="note">
        <ShieldCheckIcon aria-hidden="true" />
        <span>
          <strong>{t('mcp.securityNoteLead')}</strong> {t('mcp.securityNoteBody')}
        </span>
      </div>

      <section className="mcp-servers" aria-labelledby="mcp-servers-title">
        <header className="mcp-servers__header">
          <div>
            <h2 id="mcp-servers-title">{t('mcp.serversTitle')}</h2>
            <p>
              {workspace.servers.length
                ? tPlural('mcp.serversCount', workspace.servers.length)
                : t('mcp.serversNone')}
            </p>
          </div>
          <Button type="button" variant="outline" onClick={() => setEditor(createServerDraft())}>
            <PlusIcon />
            {t('mcp.addServer')}
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
                        {server.oauth ? <span>{t('mcp.rowOauth')}</span> : null}
                        {presentation.toolCount ? (
                          <span>{tPlural('mcp.rowToolCount', presentation.toolCount)}</span>
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
                            ? t('mcp.rowConnecting')
                            : server.oauth && !status?.authenticated
                              ? t('mcp.rowAuthorize')
                              : t('mcp.rowConnect')}
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
                          {actionBusy ? t('mcp.rowDisconnecting') : t('mcp.rowDisconnect')}
                        </Button>
                      ) : null}
                      <Button
                        type="button"
                        size="icon-sm"
                        variant="ghost"
                        aria-label={t('mcp.rowEdit', { name: server.name })}
                        title={t('mcp.rowEdit', { name: server.name })}
                        onClick={() => editServer(server)}
                      >
                        <PencilIcon />
                      </Button>
                      <Button
                        type="button"
                        size="icon-sm"
                        variant="ghost"
                        className="mcp-server-row__remove"
                        aria-label={t('mcp.rowRemove', { name: server.name })}
                        title={t('mcp.rowRemove', { name: server.name })}
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
            <h3>{t('mcp.emptyTitle')}</h3>
            <p>{t('mcp.emptyBody')}</p>
            <Button type="button" variant="outline" onClick={() => setEditor(createServerDraft())}>
              <PlusIcon />
              {t('mcp.emptyAction')}
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
