import { spawn as nodeSpawn, type ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'
import type { GatewayStatus } from '@shared/gateway'
import type { GatewaySettings } from '@shared/settings'
import {
  IDLE_ENGINE,
  isNewer,
  type Availability,
  type EngineUpdateResult,
  type EngineUpdateState,
} from '@shared/updates'
import { DEFAULT_FALLBACK_DIRS, locateCli } from '../gateway/cli-locator'

type Listener = (state: EngineUpdateState) => void

/** A marker older than this belongs to a run that died; ignore and remove it. */
export const MARKER_MAX_AGE_MS = 20 * 60 * 1000
const CHECK_TIMEOUT_MS = 20_000
const INSTALL_TIMEOUT_MS = 10 * 60 * 1000
const MAX_LOG_LINES = 200

export interface RunResult {
  code: number | null
  timedOut: boolean
  stdout: string
  stderr: string
}

export interface Spawner {
  (
    command: string,
    args: string[],
    options: { env: NodeJS.ProcessEnv; detached: boolean; stdio: ['ignore', 'pipe', 'pipe'] },
  ): ChildProcess
}

export interface EngineUpdaterDeps {
  getSettings: () => GatewaySettings
  gateway: { current(): GatewayStatus; restart(): Promise<GatewayStatus> }
  /** Where the in-progress marker lives; the app's userData by default. */
  markerPath: string
  locate?: (override: string | null) => string | null
  spawn?: Spawner
  isPidAlive?: (pid: number) => boolean
  now?: () => number
  /** Stops the installer's process tree; injectable so tests never signal real pids. */
  kill?: (child: ChildProcess) => void
}

interface Marker {
  pid: number
  startedAt: number
}

/**
 * Upgrades the engine the way a terminal would, by running `agentos upgrade`,
 * and adds the two things a terminal cannot: it streams the installer's
 * output into the About pane, and it restarts the gateway this app spawned
 * (`--no-restart` keeps the CLI's own restart out of the supervisor's way).
 *
 * The renderer, which already speaks the gateway's protocol, confirms the
 * restarted gateway reports the new version and runs the post-upgrade data
 * check; main only knows processes.
 */
export class EngineUpdater {
  private state: EngineUpdateState = { ...IDLE_ENGINE }
  private readonly listeners = new Set<Listener>()
  private child: ChildProcess | null = null

  private readonly locate: (override: string | null) => string | null
  private readonly spawn: Spawner
  private readonly isPidAlive: (pid: number) => boolean
  private readonly now: () => number
  private readonly kill: (child: ChildProcess) => void

  constructor(private readonly deps: EngineUpdaterDeps) {
    this.locate = deps.locate ?? ((override) => locateCli({ override }))
    this.spawn = deps.spawn ?? (nodeSpawn as unknown as Spawner)
    this.isPidAlive = deps.isPidAlive ?? defaultPidAlive
    this.now = deps.now ?? Date.now
    this.kill = deps.kill ?? killTree
  }

  current(): EngineUpdateState {
    return { ...this.state, log: [...this.state.log] }
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  /**
   * Called once at boot. A marker left on disk means a previous run never
   * reached its end: the installer may still be alive (the app was quit while
   * it ran) or it died with the app. Either way the user should re-check
   * rather than trust the last "done".
   */
  recover(): EngineUpdateState {
    const marker = this.readMarker()
    if (!marker) return this.current()
    const stale = this.now() - marker.startedAt > MARKER_MAX_AGE_MS || !this.isPidAlive(marker.pid)
    if (stale) this.clearMarker()
    return this.set({
      ...this.state,
      interrupted: true,
      error: stale
        ? 'A previous engine update did not finish. Check for updates to see where it left off.'
        : 'An engine update from a previous launch is still running. Wait for it to finish, then check again.',
    })
  }

  async check(): Promise<EngineUpdateState> {
    if (this.busy()) return this.current()
    const cli = this.locate(this.deps.getSettings().cliPath)
    if (!cli) {
      return this.set({ ...this.state, phase: 'error', error: 'agentos CLI not found.' })
    }
    this.set({ ...this.state, phase: 'checking', error: null, interrupted: false })
    const run = await this.run(cli, ['upgrade', '--check', '--json'], CHECK_TIMEOUT_MS)
    const payload = lastJson(run.stdout)
    if (run.timedOut || run.code !== 0 || !payload) {
      return this.set({
        ...this.state,
        phase: 'error',
        error: run.timedOut
          ? 'Checking for updates timed out.'
          : `Could not check for updates (exit ${run.code ?? 'null'}).${tail(run.stderr)}`,
      })
    }
    const availability = payload.status as Availability | undefined
    return this.set({
      ...this.state,
      phase: this.state.result ? 'done' : 'idle',
      current: str(payload.current),
      latest: str(payload.latest),
      availability: availability ?? 'offline',
      checkedAt: this.now(),
    })
  }

  async apply(): Promise<EngineUpdateState> {
    if (this.busy()) return this.current()
    const settings = this.deps.getSettings()
    if (settings.mode === 'external') {
      return this.set({
        ...this.state,
        phase: 'error',
        error:
          'The gateway runs outside this app (external mode). Upgrade it where it runs with `agentos upgrade`.',
      })
    }
    const cli = this.locate(settings.cliPath)
    if (!cli) {
      return this.set({ ...this.state, phase: 'error', error: 'agentos CLI not found.' })
    }

    this.set({
      ...this.state,
      phase: 'installing',
      log: [],
      error: null,
      manualCommand: null,
      result: null,
      interrupted: false,
    })
    const run = await this.run(
      cli,
      ['upgrade', '--json', '--no-restart'],
      INSTALL_TIMEOUT_MS,
      (line) => this.appendLog(line),
      (pid) => this.writeMarker(pid),
    )
    const payload = lastJson(run.stdout)

    if (run.timedOut) {
      this.clearMarker()
      return this.set({
        ...this.state,
        phase: 'error',
        error:
          'The installer timed out and was stopped. Run `agentos upgrade` in a terminal to retry.',
      })
    }
    if (run.code === 3) {
      this.clearMarker()
      return this.set({
        ...this.state,
        phase: 'error',
        manualCommand: str(payload?.manualCommand),
        error: 'This install cannot be upgraded by the app. Run the command below in a terminal.',
      })
    }
    if (run.code !== 0 || !payload) {
      this.clearMarker()
      return this.set({
        ...this.state,
        phase: 'error',
        error: `Upgrade failed (exit ${run.code ?? 'null'}).${tail(run.stderr)}`,
      })
    }

    const result: EngineUpdateResult = {
      old: str(payload.old) ?? this.state.current ?? '',
      new: str(payload.new) ?? '',
      gatewayRestarted: false,
      snapshot: str((payload.snapshot as Record<string, unknown> | null)?.path),
      source: payload.source === 'github' ? 'github' : payload.source === 'pypi' ? 'pypi' : null,
    }
    this.appendLog(`Installed ${result.old} → ${result.new}.`)

    const gateway = this.deps.gateway.current()
    if (gateway.pid !== null && (gateway.state === 'running' || gateway.state === 'starting')) {
      this.set({ ...this.state, phase: 'restarting', result })
      this.appendLog('Restarting the gateway…')
      const after = await this.deps.gateway.restart()
      if (after.state !== 'running') {
        this.clearMarker()
        return this.set({
          ...this.state,
          phase: 'error',
          result,
          error: `The package is upgraded but the gateway did not come back: ${after.error ?? after.state}.`,
        })
      }
      result.gatewayRestarted = true
      this.appendLog('Gateway restarted.')
    } else if (gateway.state === 'running') {
      this.appendLog('The running gateway was not started by this app; restart it to apply.')
    }

    this.clearMarker()
    const latest = this.state.latest
    return this.set({
      ...this.state,
      phase: 'done',
      result,
      current: result.new || this.state.current,
      availability: latest && result.new && isNewer(latest, result.new) ? 'outdated' : 'up-to-date',
    })
  }

  private busy(): boolean {
    return (
      this.state.phase === 'checking' ||
      this.state.phase === 'installing' ||
      this.state.phase === 'restarting'
    )
  }

  private run(
    cli: string,
    args: string[],
    timeoutMs: number,
    onLine?: (line: string) => void,
    onSpawn?: (pid: number) => void,
  ): Promise<RunResult> {
    return new Promise((resolve) => {
      let child: ChildProcess
      try {
        child = this.spawn(cli, args, {
          env: hardenedEnv(),
          // Own process group, so a timeout kills uv and everything it spawned.
          detached: process.platform !== 'win32',
          stdio: ['ignore', 'pipe', 'pipe'],
        })
      } catch (err) {
        resolve({ code: null, timedOut: false, stdout: '', stderr: String(err) })
        return
      }
      this.child = child
      if (child.pid !== undefined) onSpawn?.(child.pid)

      let stdout = ''
      let stderr = ''
      let timedOut = false
      const outLines = lineSplitter((line) => onLine?.(line))
      const errLines = lineSplitter((line) => {
        if (!isLogNoise(line)) onLine?.(line)
      })
      child.stdout?.on('data', (chunk: Buffer) => {
        stdout += chunk.toString()
        outLines(chunk.toString())
      })
      child.stderr?.on('data', (chunk: Buffer) => {
        stderr += chunk.toString()
        errLines(chunk.toString())
      })

      const timer = setTimeout(() => {
        timedOut = true
        this.kill(child)
      }, timeoutMs)

      const finish = (code: number | null) => {
        clearTimeout(timer)
        if (this.child === child) this.child = null
        resolve({ code, timedOut, stdout, stderr })
      }
      child.once('error', (err) => {
        stderr += `\n${err.message}`
        finish(null)
      })
      child.once('exit', (code) => finish(code))
    })
  }

  private appendLog(line: string): void {
    const text = line.trimEnd()
    if (!text) return
    const log = [...this.state.log, text].slice(-MAX_LOG_LINES)
    this.set({ ...this.state, log })
  }

  private readMarker(): Marker | null {
    try {
      const raw = JSON.parse(readFileSync(this.deps.markerPath, 'utf8')) as Partial<Marker>
      if (typeof raw.pid === 'number' && typeof raw.startedAt === 'number') {
        return { pid: raw.pid, startedAt: raw.startedAt }
      }
    } catch {
      /* no marker, or unreadable: same thing */
    }
    return null
  }

  private writeMarker(pid: number): void {
    try {
      mkdirSync(path.dirname(this.deps.markerPath), { recursive: true })
      const marker: Marker = { pid, startedAt: this.now() }
      writeFileSync(this.deps.markerPath, JSON.stringify(marker))
    } catch {
      /* best effort: a missing marker only weakens crash recovery */
    }
  }

  private clearMarker(): void {
    try {
      if (existsSync(this.deps.markerPath)) unlinkSync(this.deps.markerPath)
    } catch {
      /* ignore */
    }
  }

  private set(next: EngineUpdateState): EngineUpdateState {
    this.state = next
    const copy = this.current()
    for (const fn of this.listeners) fn(copy)
    return copy
  }
}

/** Marker location: next to the gateway's own state, so a CLI can see it too. */
export function defaultMarkerPath(env: NodeJS.ProcessEnv = process.env): string {
  const override = env.AGENTOS_STATE_DIR?.trim()
  const root = override
    ? override.startsWith('~/')
      ? path.join(homedir(), override.slice(2))
      : override
    : path.join(homedir(), '.agentos')
  return path.join(root, 'state', 'desktop', 'engine-update.json')
}

/**
 * GUI apps launch with a bare PATH: add the login-shell locations so the CLI
 * finds `uv`, and silence the CLI's own once-a-day update notice so it never
 * ends up in the progress log.
 */
export function hardenedEnv(base: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const entries = (base.PATH ?? '').split(path.delimiter).filter(Boolean)
  for (const dir of [...DEFAULT_FALLBACK_DIRS, '/usr/bin', '/bin']) {
    if (!entries.includes(dir)) entries.push(dir)
  }
  return { ...base, PATH: entries.join(path.delimiter), AGENTOS_NO_UPDATE_NOTICE: '1' }
}

/** The `--json` object is the last `{…}` line on stdout; prose may precede it. */
export function lastJson(stdout: string): Record<string, unknown> | null {
  const lines = stdout.split(/\r?\n/).map((l) => l.trim())
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i]!
    if (!line.startsWith('{')) continue
    try {
      const parsed: unknown = JSON.parse(line)
      if (parsed && typeof parsed === 'object') return parsed as Record<string, unknown>
    } catch {
      /* not this line */
    }
  }
  return null
}

function lineSplitter(onLine: (line: string) => void): (chunk: string) => void {
  let buffer = ''
  return (chunk) => {
    buffer += chunk
    const parts = buffer.split(/\r?\n/)
    buffer = parts.pop() ?? ''
    for (const part of parts) onLine(part)
  }
}

/** structlog's env-loading chatter (`[debug ] env.loaded …`) is not progress. */
function isLogNoise(line: string): boolean {
  return /\[(debug|info)\s*\]/.test(line)
}

function tail(stderr: string): string {
  const lines = stderr
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !isLogNoise(l))
    .slice(-3)
  return lines.length ? `\n${lines.join('\n')}` : ''
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

function killTree(child: ChildProcess): void {
  if (child.pid === undefined) return
  try {
    if (process.platform !== 'win32') process.kill(-child.pid, 'SIGTERM')
    else child.kill()
  } catch {
    child.kill()
  }
  setTimeout(() => {
    try {
      if (child.pid !== undefined && process.platform !== 'win32')
        process.kill(-child.pid, 'SIGKILL')
    } catch {
      /* already gone */
    }
  }, 3000).unref()
}

function defaultPidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === 'EPERM'
  }
}
