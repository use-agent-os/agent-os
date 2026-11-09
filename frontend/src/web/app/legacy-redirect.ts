// Where each route of the previous console lives now (overhaul-control-ui D5,
// shell spec "Legacy URLs redirect"). Pure: the route table renders the result
// through <Navigate replace>, so Back never returns to the legacy URL.
import { defaultSessionKey, sessionKeyToPath } from '@/web/shell/session-url'

/** Legacy management views that moved into Settings, by legacy path. */
export const LEGACY_SETTINGS_PATHS: Readonly<Record<string, string>> = {
  health: 'health',
  channels: 'channels',
  mcp: 'mcp',
  skills: 'skills',
  memory: 'memory',
  agents: 'agents',
  env: 'environment',
  logs: 'logs',
  approvals: 'approvals',
  config: 'advanced',
  setup: 'setup',
}

/** Legacy pages that were renamed, by legacy path. */
export const LEGACY_PAGE_PATHS: Readonly<Record<string, string>> = {
  overview: 'home',
  cron: 'automations',
}

function withQuery(path: string, params: URLSearchParams): string {
  const query = params.toString()
  return query ? `${path}?${query}` : path
}

/**
 * The new location for a legacy console URL, or null when the URL is already
 * a current route. `pathname` is relative to the console base path.
 */
export function legacyRedirect(pathname: string, search = ''): string | null {
  const segments = pathname.replace(/^\/+|\/+$/g, '').split('/')
  const head = (segments[0] ?? '').toLowerCase()
  const params = new URLSearchParams(search)
  const single = segments.length === 1

  if (head in LEGACY_PAGE_PATHS && single) {
    return withQuery(`/${LEGACY_PAGE_PATHS[head]}`, params)
  }

  if (head === 'channels' && single && params.get('view') === 'setup') {
    params.delete('view')
    params.set('setup', '1')
    return withQuery('/settings/channels', params)
  }

  if (head in LEGACY_SETTINGS_PATHS && single) {
    return withQuery(`/settings/${LEGACY_SETTINGS_PATHS[head]}`, params)
  }

  if (head === 'settings' && single && params.has('step')) {
    const step = params.get('step') ?? ''
    params.delete('step')
    if (step === 'channels') {
      params.set('setup', '1')
      return withQuery('/settings/channels', params)
    }
    if (step) params.set('step', step)
    return withQuery('/settings/setup', params)
  }

  if (head === 'projects' && single && params.get('project')) {
    const project = params.get('project')!
    params.delete('project')
    return withQuery(`/projects/${encodeURIComponent(project)}`, params)
  }

  if (head === 'chat' && single) {
    const session = params.get('session')
    const agent = params.get('agent')
    if (session) {
      params.delete('session')
      params.delete('agent')
      return withQuery(sessionKeyToPath(session), params)
    }
    if (agent) {
      params.delete('agent')
      return withQuery(sessionKeyToPath(defaultSessionKey(agent)), params)
    }
  }

  return null
}
