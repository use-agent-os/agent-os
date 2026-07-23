import { describe, expect, it } from 'vitest'
import {
  ROBINHOOD_MCP_URL,
  createServerDraft,
  normalizeWorkspace,
  robinhoodPresentation,
  serverFromDraft,
  serverPresentation,
  validateServerDraft,
  type McpServerConfig,
} from './logic'

const HTTP_SERVER: McpServerConfig = {
  name: 'docs',
  transport: 'streamable_http',
  url: 'https://example.com/mcp',
  command: null,
  args: [],
  env: {},
  headers: {},
  oauth: true,
  tool_timeout_seconds: 30,
}

describe('MCP view logic', () => {
  it('normalizes missing configuration and indexes runtime status', () => {
    expect(normalizeWorkspace({}, {})).toEqual({ enabled: false, servers: [], statusByName: {} })
    expect(
      normalizeWorkspace(
        { mcp: { enabled: true, servers: [HTTP_SERVER] } },
        { servers: [{ name: 'docs', connected: true, tools: ['search'] }] },
      ),
    ).toMatchObject({
      enabled: true,
      servers: [HTTP_SERVER],
      statusByName: { docs: { connected: true, tools: ['search'] } },
    })
  })

  it('validates names, endpoints, headers, commands, and timeouts', () => {
    const invalid = createServerDraft({ name: 'bad name', url: 'ftp://example.com' })
    invalid.headers = '[]'
    invalid.timeout = '0'
    expect(validateServerDraft(invalid, [])).toEqual({
      name: 'Use letters, numbers, dots, underscores, or hyphens.',
      url: 'Use an HTTP or HTTPS URL.',
      headers: 'Headers must be a JSON object.',
      timeout: 'Choose a timeout from 1 to 600 seconds.',
    })

    const stdio = createServerDraft({ name: 'local', transport: 'stdio' })
    expect(validateServerDraft(stdio, [])).toMatchObject({ command: 'Enter a command.' })
  })

  it('builds a transport-safe server payload from the editor', () => {
    const draft = createServerDraft({ name: 'local', transport: 'stdio' })
    draft.command = 'uvx'
    draft.args = 'mcp-server --safe'
    draft.oauth = true
    expect(serverFromDraft(draft)).toMatchObject({
      name: 'local',
      command: 'uvx',
      args: ['mcp-server', '--safe'],
      url: null,
      headers: {},
      oauth: false,
    })
  })

  it('derives connection and Robinhood presentation from live status', () => {
    expect(serverPresentation(HTTP_SERVER, undefined, false).label).toBe('Paused')
    expect(
      serverPresentation(HTTP_SERVER, { name: 'docs', authenticated: false }, true).label,
    ).toBe('Authorization required')
    expect(
      serverPresentation(
        HTTP_SERVER,
        { name: 'docs', connected: true, tools: ['one', 'two'] },
        true,
      ),
    ).toMatchObject({ tone: 'connected', toolCount: 2 })

    const robinhood = { ...HTTP_SERVER, name: 'robinhood', url: ROBINHOOD_MCP_URL }
    expect(
      robinhoodPresentation(
        [robinhood],
        { robinhood: { name: 'robinhood', connected: true, tools: ['trade'] } },
        true,
      ),
    ).toMatchObject({ tone: 'connected', detail: '1 live tool', tools: '1 registered' })
  })

  it('does not claim a connection state when live MCP status is unavailable', () => {
    expect(serverPresentation(HTTP_SERVER, undefined, true, false)).toMatchObject({
      tone: 'unavailable',
      label: 'Status unavailable',
      toolCount: 0,
    })

    const robinhood = { ...HTTP_SERVER, name: 'robinhood', url: ROBINHOOD_MCP_URL }
    expect(robinhoodPresentation([robinhood], {}, true, false)).toMatchObject({
      tone: 'unavailable',
      label: 'Status unavailable',
      action: 'Review connection',
    })
  })
})
