import { t, tPlural } from '@/i18n'
import '@/i18n/en/mcp'

export const ROBINHOOD_MCP_URL = 'https://agent.robinhood.com/mcp/trading'
export const ROBINHOOD_HELP_URL =
  'https://robinhood.com/us/en/support/articles/agentic-trading-overview/#ConnectyourAIagent'

export const BASE_MCP_URL = 'https://mcp.base.org'
export const BASE_HELP_URL = 'https://docs.base.org/agents'
export const BASE_SERVER_NAME = 'base-mcp'

export type McpPartnerId = 'robinhood' | 'base'

export interface McpPartner {
  id: McpPartnerId
  name: string
  serverName: string
  url: string
  helpUrl: string
}

export const MCP_PARTNERS: readonly McpPartner[] = [
  {
    id: 'robinhood',
    name: 'Robinhood',
    serverName: 'robinhood-trading',
    url: ROBINHOOD_MCP_URL,
    helpUrl: ROBINHOOD_HELP_URL,
  },
  {
    id: 'base',
    name: 'Base',
    serverName: BASE_SERVER_NAME,
    url: BASE_MCP_URL,
    helpUrl: BASE_HELP_URL,
  },
] as const

export type McpTransport = 'streamable_http' | 'sse' | 'stdio'

export interface McpServerConfig {
  name: string
  transport: McpTransport
  url: string | null
  command: string | null
  args: string[]
  env: Record<string, string>
  headers: Record<string, string>
  oauth: boolean
  tool_timeout_seconds: number
}

export interface McpServerStatus {
  name: string
  transport?: McpTransport
  url?: string | null
  oauth?: boolean
  authenticated?: boolean
  connected?: boolean
  tools?: string[]
}

export interface McpConfigResponse {
  mcp?: {
    enabled?: boolean
    servers?: McpServerConfig[]
  }
}

export interface McpStatusResponse {
  enabled?: boolean
  servers?: McpServerStatus[]
}

export interface McpWorkspace {
  enabled: boolean
  servers: McpServerConfig[]
  statusByName: Record<string, McpServerStatus>
}

export interface McpServerDraft {
  originalName: string | null
  name: string
  transport: McpTransport
  url: string
  command: string
  args: string
  env: Record<string, string>
  headers: string
  oauth: boolean
  timeout: string
}

export interface McpDraftErrors {
  name?: string
  url?: string
  command?: string
  headers?: string
  timeout?: string
}

export type McpServerTone = 'connected' | 'authorization' | 'paused' | 'offline' | 'unavailable'

export interface McpServerPresentation {
  tone: McpServerTone
  label: string
  detail: string
  toolCount: number
}

export interface PartnerPresentation {
  tone: 'connected' | 'authorization' | 'paused' | 'ready' | 'unavailable'
  label: string
  detail: string
  tools: string
  action: string
}

/** @deprecated Use PartnerPresentation. */
export type RobinhoodPresentation = PartnerPresentation

export function normalizeWorkspace(
  config: McpConfigResponse | null | undefined,
  status: McpStatusResponse | null | undefined,
): McpWorkspace {
  const servers = Array.isArray(config?.mcp?.servers) ? config.mcp.servers : []
  const statusByName = Object.fromEntries(
    (Array.isArray(status?.servers) ? status.servers : []).map((entry) => [entry.name, entry]),
  )
  return {
    enabled: Boolean(config?.mcp?.enabled),
    servers,
    statusByName,
  }
}

export function createServerDraft(
  server?: Partial<McpServerConfig> & { originalName?: string | null },
): McpServerDraft {
  return {
    originalName: server?.originalName ?? null,
    name: server?.name ?? '',
    transport: server?.transport ?? 'streamable_http',
    url: server?.url ?? '',
    command: server?.command ?? '',
    args: server?.args?.join(' ') ?? '',
    env: server?.env ?? {},
    headers: JSON.stringify(server?.headers ?? {}, null, 2),
    oauth: Boolean(server?.oauth),
    timeout: String(server?.tool_timeout_seconds ?? 30),
  }
}

export function validateServerDraft(
  draft: McpServerDraft,
  servers: McpServerConfig[],
): McpDraftErrors {
  const errors: McpDraftErrors = {}
  const name = draft.name.trim()
  if (!name) errors.name = t('mcp.errorName')
  else if (!/^[a-zA-Z0-9._-]+$/.test(name)) {
    errors.name = t('mcp.errorNameCharset')
  } else if (servers.some((server) => server.name === name && server.name !== draft.originalName)) {
    errors.name = t('mcp.errorNameTaken')
  }

  if (draft.transport === 'stdio') {
    if (!draft.command.trim()) errors.command = t('mcp.errorCommand')
  } else {
    try {
      const url = new URL(draft.url.trim())
      if (!['http:', 'https:'].includes(url.protocol)) {
        errors.url = t('mcp.errorUrlScheme')
      }
    } catch {
      errors.url = t('mcp.errorUrl')
    }
  }

  try {
    const headers = JSON.parse(draft.headers || '{}') as unknown
    if (!headers || Array.isArray(headers) || typeof headers !== 'object') {
      errors.headers = t('mcp.errorHeadersObject')
    } else if (Object.values(headers).some((value) => typeof value !== 'string')) {
      errors.headers = t('mcp.errorHeaderValues')
    }
  } catch {
    errors.headers = t('mcp.errorHeadersJson')
  }

  const timeout = Number(draft.timeout)
  if (!Number.isFinite(timeout) || timeout < 1 || timeout > 600) {
    errors.timeout = t('mcp.errorTimeout')
  }
  return errors
}

export function serverFromDraft(draft: McpServerDraft): McpServerConfig {
  const stdio = draft.transport === 'stdio'
  return {
    name: draft.name.trim(),
    transport: draft.transport,
    command: stdio ? draft.command.trim() : null,
    args: stdio ? draft.args.trim().split(/\s+/).filter(Boolean) : [],
    url: stdio ? null : draft.url.trim(),
    env: draft.env,
    headers: stdio ? {} : (JSON.parse(draft.headers || '{}') as Record<string, string>),
    oauth: draft.transport === 'streamable_http' && draft.oauth,
    tool_timeout_seconds: Number(draft.timeout) || 30,
  }
}

export function transportLabel(transport: McpTransport): string {
  if (transport === 'streamable_http') return t('mcp.transportStreamableHttp')
  if (transport === 'sse') return t('mcp.transportSse')
  return t('mcp.transportStdio')
}

export function serverDetail(server: McpServerConfig): string {
  if (server.transport !== 'stdio') return server.url || t('mcp.detailIncomplete')
  return [server.command, ...server.args].filter(Boolean).join(' ') || t('mcp.detailIncomplete')
}

export function serverPresentation(
  server: McpServerConfig,
  status: McpServerStatus | undefined,
  enabled: boolean,
  statusAvailable = true,
): McpServerPresentation {
  const toolCount = status?.tools?.length ?? 0
  if (!enabled)
    return {
      tone: 'paused',
      label: t('mcp.statePaused'),
      detail: t('mcp.statePausedDetail'),
      toolCount,
    }
  if (!statusAvailable) {
    return {
      tone: 'unavailable',
      label: t('mcp.stateUnavailable'),
      detail: t('mcp.stateUnavailableDetail'),
      toolCount,
    }
  }
  if (status?.connected) {
    return {
      tone: 'connected',
      label: t('mcp.stateConnected'),
      detail: tPlural('mcp.stateConnectedDetail', toolCount),
      toolCount,
    }
  }
  if (server.oauth && !status?.authenticated) {
    return {
      tone: 'authorization',
      label: t('mcp.stateAuthorization'),
      detail: t('mcp.stateAuthorizationDetail'),
      toolCount,
    }
  }
  return {
    tone: 'offline',
    label: t('mcp.stateOffline'),
    detail: t('mcp.stateOfflineDetail'),
    toolCount,
  }
}

type PartnerCopy = {
  ready: string
  readyDetail: string
  readyTools: string
  readyAction: string
  paused: string
  pausedDetail: string
  pausedTools: string
  reviewAction: string
  unavailable: string
  unavailableDetail: string
  unavailableTools: string
  connected: string
  connectedTools: (count: number) => string
  connectedDetail: (count: number) => string
  manageAction: string
  oauthRequired: string
  oauthDetail: string
  oauthTools: string
  authorizeAction: string
  savedDetail: string
}

function partnerCopy(partner: McpPartner): PartnerCopy {
  if (partner.id === 'base') {
    return {
      ready: t('mcp.baseReady'),
      readyDetail: t('mcp.baseReadyDetail'),
      readyTools: t('mcp.baseReadyTools'),
      readyAction: t('mcp.baseReadyAction'),
      paused: t('mcp.basePaused'),
      pausedDetail: t('mcp.basePausedDetail'),
      pausedTools: t('mcp.basePausedTools'),
      reviewAction: t('mcp.baseReviewAction'),
      unavailable: t('mcp.baseUnavailable'),
      unavailableDetail: t('mcp.baseUnavailableDetail'),
      unavailableTools: t('mcp.baseUnavailableTools'),
      connected: t('mcp.baseConnected'),
      connectedTools: (count) => t('mcp.baseConnectedTools', { count }),
      connectedDetail: (count) => tPlural('mcp.baseConnectedDetail', count),
      manageAction: t('mcp.baseManageAction'),
      oauthRequired: t('mcp.baseOauthRequired'),
      oauthDetail: t('mcp.baseOauthDetail'),
      oauthTools: t('mcp.baseOauthTools'),
      authorizeAction: t('mcp.baseAuthorizeAction'),
      savedDetail: t('mcp.baseSavedDetail'),
    }
  }
  return {
    ready: t('mcp.rhReady'),
    readyDetail: t('mcp.rhReadyDetail'),
    readyTools: t('mcp.rhReadyTools'),
    readyAction: t('mcp.rhReadyAction'),
    paused: t('mcp.rhPaused'),
    pausedDetail: t('mcp.rhPausedDetail'),
    pausedTools: t('mcp.rhPausedTools'),
    reviewAction: t('mcp.rhReviewAction'),
    unavailable: t('mcp.rhUnavailable'),
    unavailableDetail: t('mcp.rhUnavailableDetail'),
    unavailableTools: t('mcp.rhUnavailableTools'),
    connected: t('mcp.rhConnected'),
    connectedTools: (count) => t('mcp.rhConnectedTools', { count }),
    connectedDetail: (count) => tPlural('mcp.rhConnectedDetail', count),
    manageAction: t('mcp.rhManageAction'),
    oauthRequired: t('mcp.rhOauthRequired'),
    oauthDetail: t('mcp.rhOauthDetail'),
    oauthTools: t('mcp.rhOauthTools'),
    authorizeAction: t('mcp.rhAuthorizeAction'),
    savedDetail: t('mcp.rhSavedDetail'),
  }
}

export function partnerPresentation(
  partner: McpPartner,
  servers: McpServerConfig[],
  statusByName: Record<string, McpServerStatus>,
  enabled: boolean,
  statusAvailable = true,
): PartnerPresentation {
  const copy = partnerCopy(partner)
  const server = servers.find((entry) => entry.url === partner.url)
  if (!server) {
    return {
      tone: 'ready',
      label: copy.ready,
      detail: copy.readyDetail,
      tools: copy.readyTools,
      action: copy.readyAction,
    }
  }
  const status = statusByName[server.name]
  const toolCount = status?.tools?.length ?? 0
  if (!enabled) {
    return {
      tone: 'paused',
      label: copy.paused,
      detail: copy.pausedDetail,
      tools: copy.pausedTools,
      action: copy.reviewAction,
    }
  }
  if (!statusAvailable) {
    return {
      tone: 'unavailable',
      label: copy.unavailable,
      detail: copy.unavailableDetail,
      tools: copy.unavailableTools,
      action: copy.reviewAction,
    }
  }
  if (status?.connected) {
    return {
      tone: 'connected',
      label: copy.connected,
      detail: copy.connectedDetail(toolCount),
      tools: copy.connectedTools(toolCount),
      action: copy.manageAction,
    }
  }
  if (server.oauth && !status?.authenticated) {
    return {
      tone: 'authorization',
      label: copy.oauthRequired,
      detail: copy.oauthDetail,
      tools: copy.oauthTools,
      action: copy.authorizeAction,
    }
  }
  return {
    tone: 'ready',
    label: copy.ready,
    detail: copy.savedDetail,
    tools: copy.readyTools,
    action: copy.reviewAction,
  }
}

/** Convenience wrapper kept for existing callers. */
export function robinhoodPresentation(
  servers: McpServerConfig[],
  statusByName: Record<string, McpServerStatus>,
  enabled: boolean,
  statusAvailable = true,
): PartnerPresentation {
  const robinhood = MCP_PARTNERS.find((partner) => partner.id === 'robinhood')
  if (!robinhood) throw new Error('Robinhood partner is not registered')
  return partnerPresentation(robinhood, servers, statusByName, enabled, statusAvailable)
}
