/**
 * Two things can be out of date, and they update differently:
 *
 * - the **engine** — the `use-agent-os` Python package the app spawns as
 *   `agentos gateway run`. Main runs `agentos upgrade` for it and restarts the
 *   managed gateway; the renderer then confirms the new version over RPC.
 * - the **app** — this Electron shell. `electron-updater` downloads the next
 *   signed build from GitHub Releases and swaps it in on relaunch.
 */

export type Availability = 'outdated' | 'up-to-date' | 'offline'

export type EnginePhase =
  | 'idle'
  | 'checking'
  | 'installing'
  | 'restarting'
  /** Install done; the gateway (if any) is back up on the new package. */
  | 'done'
  | 'error'

export interface EngineUpdateResult {
  old: string
  new: string
  /** Whether main restarted a gateway it manages. */
  gatewayRestarted: boolean
  /** Pre-upgrade snapshot directory, when one was written. */
  snapshot: string | null
  /** Where the wheel came from. */
  source: 'pypi' | 'github' | null
}

export interface EngineUpdateState {
  phase: EnginePhase
  /** Installed CLI version, as `agentos upgrade --check` reports it. */
  current: string | null
  latest: string | null
  availability: Availability | null
  checkedAt: number | null
  /** Progress lines from the running installer, oldest first. */
  log: string[]
  /** Set when this install method cannot be upgraded by the app (exit 3). */
  manualCommand: string | null
  error: string | null
  result: EngineUpdateResult | null
  /** A previous run left a marker behind: it was interrupted mid-install. */
  interrupted: boolean
}

export const IDLE_ENGINE: EngineUpdateState = {
  phase: 'idle',
  current: null,
  latest: null,
  availability: null,
  checkedAt: null,
  log: [],
  manualCommand: null,
  error: null,
  result: null,
  interrupted: false,
}

export type AppUpdatePhase =
  /** Dev build or no publish channel: nothing to update from. */
  | 'unsupported'
  | 'idle'
  | 'checking'
  | 'up-to-date'
  | 'available'
  | 'downloading'
  /** Installed on the next relaunch. */
  | 'downloaded'
  | 'error'

export interface AppUpdateState {
  phase: AppUpdatePhase
  current: string
  latest: string | null
  /** Download progress, 0–100, while `downloading`. */
  percent: number | null
  checkedAt: number | null
  error: string | null
}

export function idleAppState(current: string): AppUpdateState {
  return { phase: 'idle', current, latest: null, percent: null, checkedAt: null, error: null }
}

/**
 * The oldest engine this renderer speaks to. Bumped whenever the desktop
 * starts depending on a gateway RPC or field the previous release lacks, so an
 * app that auto-updated ahead of the engine says so instead of half-working.
 */
export const MIN_GATEWAY_VERSION = '2026.9.9'

/**
 * Compare two AgentOS versions (CalVer `YYYY.M.D[.postN]`, or the app's
 * identical scheme). Numeric segments compare numerically; a missing segment
 * counts as zero; anything non-numeric after the numbers (`rc1`, `+local`)
 * sorts below the plain release. Returns <0, 0, >0.
 */
export function compareVersions(a: string, b: string): number {
  const pa = parseVersion(a)
  const pb = parseVersion(b)
  const len = Math.max(pa.numbers.length, pb.numbers.length)
  for (let i = 0; i < len; i++) {
    const diff = (pa.numbers[i] ?? 0) - (pb.numbers[i] ?? 0)
    if (diff !== 0) return diff
  }
  if (pa.pre === pb.pre) return 0
  if (pa.pre === '') return 1
  if (pb.pre === '') return -1
  return pa.pre < pb.pre ? -1 : 1
}

function parseVersion(raw: string): { numbers: number[]; pre: string } {
  const text = raw.trim().replace(/^v/i, '')
  const match = /^(\d+(?:\.\d+)*)(?:\.post(\d+))?(.*)$/.exec(text)
  if (!match) return { numbers: [], pre: text }
  const numbers = match[1]!.split('.').map((n) => Number(n))
  // `.postN` is "after the release": fold it in as an extra segment so
  // 2026.9.9.post1 > 2026.9.9 but < 2026.9.10.
  if (match[2] !== undefined) {
    while (numbers.length < 3) numbers.push(0)
    numbers.push(Number(match[2]))
  }
  return { numbers, pre: (match[3] ?? '').replace(/^[-+]/, '') }
}

export function isNewer(candidate: string, than: string): boolean {
  return compareVersions(candidate, than) > 0
}

/** Whether the renderer can drive `gatewayVersion` at all. */
export function gatewaySupported(gatewayVersion: string | null | undefined): boolean {
  if (!gatewayVersion) return true // unknown: do not alarm
  return compareVersions(gatewayVersion.split('+')[0] ?? gatewayVersion, MIN_GATEWAY_VERSION) >= 0
}
