/**
 * First-run engine install ("bootstrap"), modelled on the Hermes Agent
 * installer: the app drives `install.sh` stage by stage over its JSON
 * protocol (`--manifest`, `--stage NAME --json`) and shows real progress.
 *
 * Every launch first *discovers* the engine; only a missing or older engine
 * triggers the bootstrap, which installs the exact version this app was
 * built against. A newer engine is left alone.
 */

export interface StageInfo {
  name: string
  title: string
  category: string
  needsUserInput: boolean
}

export type StageState = 'pending' | 'running' | 'succeeded' | 'skipped' | 'failed'

export interface StageProgress extends StageInfo {
  state: StageState
  startedAt: number | null
  durationMs: number | null
  reason: string | null
}

export interface LogLine {
  stage: string | null
  stream: 'stdout' | 'stderr'
  line: string
}

/** How the engine was (or was not) found at launch. */
export type EngineSource =
  /** `gateway.cliPath` in settings: the user's explicit choice, used as-is. */
  | 'override'
  /** Found on PATH or in the usual install dirs. */
  | 'found'
  /** Nothing runnable anywhere. */
  | 'missing'

export interface EngineDiscovery {
  source: EngineSource
  /** Absolute path of the CLI, when one runs. */
  cliPath: string | null
  /** Its version, when it could be read (`agentos --version`). */
  version: string | null
  /** This app's version: what the bootstrap installs. */
  appVersion: string
  /** `older` / `same` / `newer` relative to the app, or null when unknown. */
  relation: 'older' | 'same' | 'newer' | null
  /** Whether the app should (re)install the engine before starting a gateway. */
  needsInstall: boolean
  /** Why, in a sentence the overlay can show. */
  reason: string
}

export type BootstrapPhase =
  /** Not decided yet (discovery running). */
  | 'checking'
  /** The engine is fine; nothing to show. */
  | 'ready'
  /** Waiting for the user: install here, or connect to an existing gateway. */
  | 'choice'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export interface BootstrapState {
  phase: BootstrapPhase
  discovery: EngineDiscovery | null
  /** `install` (fresh) or `update` (an older engine was found). */
  mode: 'install' | 'update'
  stages: StageProgress[]
  /** Bounded ring of installer output, oldest first. */
  log: LogLine[]
  /** Per-run forensic log on disk, when one was opened. */
  logPath: string | null
  error: string | null
  startedAt: number | null
  finishedAt: number | null
}

export const INITIAL_BOOTSTRAP: BootstrapState = {
  phase: 'checking',
  discovery: null,
  mode: 'install',
  stages: [],
  log: [],
  logPath: null,
  error: null,
  startedAt: null,
  finishedAt: null,
}

/** Keep the renderer's copy of the log small; the file on disk has it all. */
export const BOOTSTRAP_LOG_RING = 500

/** Install progress as a fraction: the running stage counts as half done. */
export function bootstrapProgress(stages: StageProgress[]): number {
  if (stages.length === 0) return 0
  let done = 0
  for (const s of stages) {
    if (s.state === 'succeeded' || s.state === 'skipped' || s.state === 'failed') done += 1
    else if (s.state === 'running') done += 0.5
  }
  return Math.min(1, done / stages.length)
}
