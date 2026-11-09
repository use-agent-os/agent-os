// Session key <-> chat URL codec (overhaul-control-ui D5, shell spec
// "Session keys round-trip through chat URLs").
//
//   agent:<a>:<p1>:<p2>…   <->  /chat/<enc a>/<enc p1>/<enc p2>…
//   anything else          <->  /chat/~/<enc whole key>
//
// Each part is percent-encoded on its own, so `/`, `%`, `?`, `#` and spaces in
// a part never change the path structure. `~` is not a valid normalized agent
// id, so the reserved form can never collide with a real agent.

export const RESERVED_AGENT_SEGMENT = '~'
const AGENT_ID = /^[a-z0-9_-]+$/

// URL parsers resolve `.` and `..` segments — even as `%2E` — before the app
// sees them. A dot-only part therefore gets a raw `;` suffix, a character
// encodeURIComponent always escapes, so the marker cannot be confused with data.
const DOT_ONLY = /^\.+$/
const DOT_ONLY_MARKED = /^\.+;$/

function encodePart(part: string): string {
  if (DOT_ONLY.test(part)) return `${part};`
  return encodeURIComponent(part)
}

function decodePart(segment: string): string | null {
  if (DOT_ONLY_MARKED.test(segment)) return segment.slice(0, -1)
  try {
    return decodeURIComponent(segment)
  } catch {
    return null
  }
}

/** The chat path (relative to the console base) for a session key. */
export function sessionKeyToPath(key: string): string {
  const parts = key.split(':')
  if (parts.length >= 3 && parts[0] === 'agent') {
    const [, agent, ...rest] = parts
    if (agent && AGENT_ID.test(agent) && rest.every((p) => p.length > 0)) {
      return `/chat/${encodePart(agent)}/${rest.map(encodePart).join('/')}`
    }
  }
  return `/chat/${RESERVED_AGENT_SEGMENT}/${encodePart(key)}`
}

/**
 * The session key for a chat path, or null when the path does not name one.
 * Accepts the path with or without the leading `/chat`.
 */
export function pathToSessionKey(path: string): string | null {
  const trimmed = path.replace(/^\/+/, '').replace(/[?#].*$/, '')
  const segments = trimmed.split('/')
  if (segments[0] === 'chat') segments.shift()
  if (segments.length > 0 && segments[segments.length - 1] === '') segments.pop()
  if (segments.length < 2) return null
  const [agentSegment, ...rest] = segments as [string, ...string[]]
  if (agentSegment === RESERVED_AGENT_SEGMENT) {
    if (rest.length !== 1) return null
    const key = decodePart(rest[0]!)
    return key ? key : null
  }
  const agent = decodePart(agentSegment)
  if (!agent || !AGENT_ID.test(agent)) return null
  const decoded = rest.map(decodePart)
  if (decoded.some((p) => p === null || p === '')) return null
  return ['agent', agent, ...(decoded as string[])].join(':')
}

/** The agent id of an `agent:<id>:…` key, if it has one. */
export function agentOfSessionKey(key: string): string | null {
  const parts = key.split(':')
  return parts[0] === 'agent' && parts[1] ? parts[1] : null
}

/** The default webchat session key of an agent. */
export function defaultSessionKey(agent: string): string {
  return `agent:${agent}:webchat:default`
}
