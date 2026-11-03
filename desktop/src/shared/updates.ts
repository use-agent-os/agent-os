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
  /**
   * Why the last "Restart to update" was refused, while `downloaded`. Cleared
   * when the restart goes through or a new download starts.
   */
  blocked: AppInstallBlock | null
}

/** What a relaunch would interrupt; the renderer words each one. */
export type AppInstallBlock = 'engine-updating' | 'installer-running'

export function idleAppState(current: string): AppUpdateState {
  return {
    phase: 'idle',
    current,
    latest: null,
    percent: null,
    checkedAt: null,
    error: null,
    blocked: null,
  }
}

/** How often the app looks for a new build on its own, between focus checks. */
export const APP_UPDATE_CHECK_INTERVAL_MS = 5 * 60 * 1000

/**
 * One release covers the engine and the app, so the shell shows one notice.
 * This folds both updaters into the single thing the person can do next:
 *
 * - `available`: a newer AgentOS exists for at least one of the two. "Update"
 *   installs the engine (if it needs it) and downloads the app (if it does).
 * - `working`: the engine installer or the app download is running.
 * - `restart`: the app is downloaded; only the relaunch is left.
 * - `gateway-restart`: a newer engine is installed on disk (a terminal ran
 *   `agentos upgrade`) but the managed gateway still runs the old one.
 * - `gateway-outdated`: external run mode only. The gateway this app merely
 *   connects to runs an older release than the latest known; it is updated
 *   on its own host, so the notice has no action beyond opening About.
 * - `failed`: the app download or its restart failed while a newer build is
 *   known; "Try again" re-checks and re-downloads (the cache makes that fast).
 * - `none`: nothing to do.
 *
 * In external run mode the engine package on this machine is not the one
 * running, so its state never produces a notice: no "Update" for an outdated
 * local engine, no "Restart gateway" for a newer one on disk.
 */
export type ReleaseUpdate =
  | { kind: 'none' }
  | { kind: 'available'; version: string; engine: boolean; app: boolean }
  | { kind: 'working'; version: string; step: 'engine' | 'app'; percent: number | null }
  | { kind: 'restart'; version: string; blocked: AppInstallBlock | null }
  | { kind: 'gateway-restart'; version: string; running: string }
  | { kind: 'gateway-outdated'; version: string; running: string }
  | { kind: 'failed'; version: string; error: string }

export interface ReleaseUpdateOptions {
  /** The app only connects to the gateway (Settings › Gateway › Run mode External). */
  external?: boolean
}

/**
 * The newest release either check has seen. One release covers the engine
 * and the app, so whichever check answered is a fair reading of "latest".
 */
export function latestRelease(engine: EngineUpdateState, app: AppUpdateState): string | null {
  const known = [engine.latest, app.latest].filter((v): v is string => !!v)
  return known.sort(compareVersions).at(-1) ?? null
}

/**
 * An external gateway running behind the latest known release, as
 * `{ running, latest }`; null when it is current or nothing is known.
 */
export function gatewayBehind(
  engine: EngineUpdateState,
  app: AppUpdateState,
  gatewayVersion: string | null | undefined,
): { running: string; latest: string } | null {
  const running = gatewayVersion?.split('+')[0]
  const latest = latestRelease(engine, app)
  if (!running || !latest || !isNewer(latest, running)) return null
  return { running, latest }
}

export function releaseUpdate(
  engine: EngineUpdateState,
  app: AppUpdateState,
  gatewayVersion: string | null | undefined,
  { external = false }: ReleaseUpdateOptions = {},
): ReleaseUpdate {
  if (app.phase === 'downloaded' && app.latest) {
    return { kind: 'restart', version: app.latest, blocked: app.blocked }
  }
  if (engine.phase === 'installing' || engine.phase === 'restarting') {
    return {
      kind: 'working',
      version: engine.latest ?? app.latest ?? '',
      step: 'engine',
      percent: null,
    }
  }
  if (app.phase === 'downloading' && app.latest) {
    return { kind: 'working', version: app.latest, step: 'app', percent: app.percent }
  }
  if (app.phase === 'error' && app.latest && isNewer(app.latest, app.current)) {
    return { kind: 'failed', version: app.latest, error: app.error ?? '' }
  }
  const engineOut = !external && engine.availability === 'outdated' && !!engine.latest
  const appOut = app.phase === 'available' && !!app.latest
  if (engineOut || appOut) {
    const candidates = [engineOut ? engine.latest : null, appOut ? app.latest : null].filter(
      (v): v is string => !!v,
    )
    const version = candidates.sort(compareVersions).at(-1) ?? ''
    return { kind: 'available', version, engine: engineOut, app: appOut }
  }
  if (external) {
    const behind = gatewayBehind(engine, app, gatewayVersion)
    return behind
      ? { kind: 'gateway-outdated', version: behind.latest, running: behind.running }
      : { kind: 'none' }
  }
  const running = gatewayVersion?.split('+')[0]
  if (engine.current && running && isNewer(engine.current, running)) {
    return { kind: 'gateway-restart', version: engine.current, running }
  }
  return { kind: 'none' }
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

/**
 * The packaged app carries a semver twin of its CalVer, because
 * electron-builder and electron-updater only speak semver: `2026.9.22.post1`
 * is not one, and left alone it is rewritten to `2026.9.2-2.post1`, which
 * sorts *before* 2026.9.2. The twin folds month and day into the minor
 * number and keeps the post number as the patch, so ordering survives:
 *
 *   2026.9.22        -> 2026.922.0
 *   2026.9.22.post1  -> 2026.922.1
 *   2026.9.23        -> 2026.923.0
 *   2026.10.1        -> 2026.1001.0
 *
 * A release built before this scheme (`2026.9.20`) is smaller than any twin
 * (minor 9 < 920), so every installed app upgrades into it. The CalVer
 * itself stays in `package.json` (the release-consistency test pins it to
 * `pyproject.toml`) and rides along as `calver` in the packaged metadata;
 * `main/app-version.ts` reads that one for everything a human or the engine
 * installer sees. Returns null for a string that is not a CalVer.
 */
export function calverToSemver(calver: string): string | null {
  const match = /^v?(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\.post(\d+))?$/.exec(calver.trim())
  if (!match) return null
  const [, year, month, day, post] = match
  return `${Number(year)}.${Number(month) * 100 + Number(day)}.${Number(post ?? 0)}`
}

/** The inverse of `calverToSemver`; a non-twin (an old plain release) is returned unchanged. */
export function semverToCalver(semver: string): string {
  const match = /^v?(\d{4})\.(\d{3,4})\.(\d+)$/.exec(semver.trim())
  if (!match) return semver
  const [, year, monthDay, post] = match
  const month = Math.floor(Number(monthDay) / 100)
  const day = Number(monthDay) % 100
  if (month < 1 || month > 12 || day < 1 || day > 31) return semver
  return `${year}.${month}.${day}${Number(post) > 0 ? `.post${Number(post)}` : ''}`
}

/** Whether the renderer can drive `gatewayVersion` at all. */
export function gatewaySupported(gatewayVersion: string | null | undefined): boolean {
  if (!gatewayVersion) return true // unknown: do not alarm
  return compareVersions(gatewayVersion.split('+')[0] ?? gatewayVersion, MIN_GATEWAY_VERSION) >= 0
}
