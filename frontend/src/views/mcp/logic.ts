export const ROBINHOOD_MCP_URL = 'https://agent.robinhood.com/mcp/trading'
export const ROBINHOOD_HELP_URL =
  'https://robinhood.com/us/en/support/articles/agentic-trading-overview/#ConnectyourAIagent'

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

export interface RobinhoodPresentation {
  tone: 'connected' | 'authorization' | 'paused' | 'ready' | 'unavailable'
  label: string
  detail: string
  tools: string
  action: string
}

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
  if (!name) errors.name = 'Enter a server name.'
  else if (!/^[a-zA-Z0-9._-]+$/.test(name)) {
    errors.name = 'Use letters, numbers, dots, underscores, or hyphens.'
  } else if (servers.some((server) => server.name === name && server.name !== draft.originalName)) {
    errors.name = 'This server name already exists.'
  }

  if (draft.transport === 'stdio') {
    if (!draft.command.trim()) errors.command = 'Enter a command.'
  } else {
    try {
      const url = new URL(draft.url.trim())
      if (!['http:', 'https:'].includes(url.protocol)) {
        errors.url = 'Use an HTTP or HTTPS URL.'
      }
    } catch {
      errors.url = 'Enter a valid absolute URL.'
    }
  }

  try {
    const headers = JSON.parse(draft.headers || '{}') as unknown
    if (!headers || Array.isArray(headers) || typeof headers !== 'object') {
      errors.headers = 'Headers must be a JSON object.'
    } else if (Object.values(headers).some((value) => typeof value !== 'string')) {
      errors.headers = 'Header values must be strings.'
    }
  } catch {
    errors.headers = 'Enter valid JSON.'
  }

  const timeout = Number(draft.timeout)
  if (!Number.isFinite(timeout) || timeout < 1 || timeout > 600) {
    errors.timeout = 'Choose a timeout from 1 to 600 seconds.'
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
  if (transport === 'streamable_http') return 'Streamable HTTP'
  if (transport === 'sse') return 'SSE'
  return 'Local process'
}

export function serverDetail(server: McpServerConfig): string {
  if (server.transport !== 'stdio') return server.url || 'Configuration incomplete'
  return [server.command, ...server.args].filter(Boolean).join(' ') || 'Configuration incomplete'
}

export function serverPresentation(
  server: McpServerConfig,
  status: McpServerStatus | undefined,
  enabled: boolean,
  statusAvailable = true,
): McpServerPresentation {
  const toolCount = status?.tools?.length ?? 0
  if (!enabled) return { tone: 'paused', label: 'Paused', detail: 'Runtime disabled', toolCount }
  if (!statusAvailable) {
    return {
      tone: 'unavailable',
      label: 'Status unavailable',
      detail: 'Live gateway status is unavailable',
      toolCount,
    }
  }
  if (status?.connected) {
    return {
      tone: 'connected',
      label: 'Connected',
      detail: `${toolCount} registered tool${toolCount === 1 ? '' : 's'}`,
      toolCount,
    }
  }
  if (server.oauth && !status?.authenticated) {
    return {
      tone: 'authorization',
      label: 'Authorization required',
      detail: 'Sign in before tools can load',
      toolCount,
    }
  }
  return { tone: 'offline', label: 'Disconnected', detail: 'Ready to connect', toolCount }
}

export function robinhoodPresentation(
  servers: McpServerConfig[],
  statusByName: Record<string, McpServerStatus>,
  enabled: boolean,
  statusAvailable = true,
): RobinhoodPresentation {
  const server = servers.find((entry) => entry.url === ROBINHOOD_MCP_URL)
  if (!server) {
    return {
      tone: 'ready',
      label: 'Ready to connect',
      detail: 'Secure setup ready',
      tools: 'Discovered on connect',
      action: 'Connect Robinhood',
    }
  }
  const status = statusByName[server.name]
  const toolCount = status?.tools?.length ?? 0
  if (!enabled) {
    return {
      tone: 'paused',
      label: 'Runtime paused',
      detail: 'Configured and paused',
      tools: 'Available when enabled',
      action: 'Review connection',
    }
  }
  if (!statusAvailable) {
    return {
      tone: 'unavailable',
      label: 'Status unavailable',
      detail: 'Configuration is still available',
      tools: 'Live discovery unavailable',
      action: 'Review connection',
    }
  }
  if (status?.connected) {
    return {
      tone: 'connected',
      label: 'Connected',
      detail: `${toolCount} live tool${toolCount === 1 ? '' : 's'}`,
      tools: `${toolCount} registered`,
      action: 'Manage connection',
    }
  }
  if (server.oauth && !status?.authenticated) {
    return {
      tone: 'authorization',
      label: 'OAuth required',
      detail: 'Ready for authorization',
      tools: 'Loads after authorization',
      action: 'Authorize Robinhood',
    }
  }
  return {
    tone: 'ready',
    label: 'Ready to connect',
    detail: 'Configuration saved',
    tools: 'Discovered on connect',
    action: 'Review connection',
  }
}
