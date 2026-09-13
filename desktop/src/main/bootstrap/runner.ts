import { spawn as nodeSpawn, type ChildProcess } from 'node:child_process'
import { createWriteStream, existsSync, mkdirSync, type WriteStream } from 'node:fs'
import path from 'node:path'
import {
  BOOTSTRAP_LOG_RING,
  INITIAL_BOOTSTRAP,
  type BootstrapState,
  type EngineDiscovery,
  type LogLine,
  type StageInfo,
  type StageProgress,
} from '@shared/bootstrap'
import { hardenedEnv, lastJson } from '../updates/engine-updater'

type Listener = (state: BootstrapState) => void

/** One stage may legitimately take this long (the wheel + ONNX models). */
const STAGE_TIMEOUT_MS = 15 * 60 * 1000
const MANIFEST_TIMEOUT_MS = 30_000

export interface RunnerSpawner {
  (
    command: string,
    args: string[],
    options: {
      env: NodeJS.ProcessEnv
      cwd: string
      detached: boolean
      stdio: ['ignore', 'pipe', 'pipe']
    },
  ): ChildProcess
}

export interface BootstrapRunnerDeps {
  /** Absolute path of the bundled install.sh. */
  scriptPath: string
  /** What to install: the app's own version (`v` prefix added here). */
  version: string
  /** Where per-run forensic logs go (`~/.agentos/logs`). */
  logDir: string
  /** A stable working directory for bash; the app bundle can move under us. */
  cwd?: string
  spawn?: RunnerSpawner
  now?: () => number
  /** Stops a stage's process tree; injectable so tests never signal real pids. */
  kill?: (child: ChildProcess) => void
}

/**
 * Drives `install.sh` the way the Hermes bootstrap installer drives its
 * script: `--manifest` for the stage list, then one process per
 * `--stage NAME --json`, parsing the last JSON line of stdout as the result
 * frame and streaming every other line to the renderer and to a log file.
 *
 * One process per stage buys cheap cancellation (kill the child), exact
 * per-stage timing, and a retry that restarts at a clean boundary.
 */
export class BootstrapRunner {
  private state: BootstrapState = { ...INITIAL_BOOTSTRAP }
  private readonly listeners = new Set<Listener>()
  private child: ChildProcess | null = null
  private cancelled = false
  private logFile: WriteStream | null = null

  private readonly spawn: RunnerSpawner
  private readonly now: () => number
  private readonly kill: (child: ChildProcess) => void

  constructor(private readonly deps: BootstrapRunnerDeps) {
    this.spawn = deps.spawn ?? (nodeSpawn as unknown as RunnerSpawner)
    this.now = deps.now ?? Date.now
    this.kill = deps.kill ?? killTree
  }

  current(): BootstrapState {
    return {
      ...this.state,
      stages: this.state.stages.map((s) => ({ ...s })),
      log: [...this.state.log],
    }
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  /** Discovery decided the engine is fine: nothing to show. */
  markReady(discovery: EngineDiscovery): BootstrapState {
    return this.set({ ...INITIAL_BOOTSTRAP, phase: 'ready', discovery })
  }

  /** Discovery wants an install: wait for the user before touching anything. */
  offer(discovery: EngineDiscovery): BootstrapState {
    return this.set({
      ...INITIAL_BOOTSTRAP,
      phase: 'choice',
      discovery,
      mode: discovery.version || discovery.cliPath ? 'update' : 'install',
    })
  }

  /** The user chose an external gateway instead: stand down. */
  dismiss(): BootstrapState {
    if (this.state.phase === 'running') return this.current()
    return this.set({ ...this.state, phase: 'ready' })
  }

  cancel(): BootstrapState {
    if (this.state.phase !== 'running') return this.current()
    this.cancelled = true
    if (this.child) this.kill(this.child)
    return this.current()
  }

  async run(): Promise<BootstrapState> {
    if (this.state.phase === 'running') return this.current()
    this.cancelled = false
    this.openLog()
    this.set({
      ...this.state,
      phase: 'running',
      stages: [],
      log: [],
      error: null,
      startedAt: this.now(),
      finishedAt: null,
    })

    const manifest = await this.manifest()
    if (!manifest.ok) return this.fail(null, manifest.error)
    this.set({
      ...this.state,
      stages: manifest.stages.map((s) => ({
        ...s,
        state: 'pending',
        startedAt: null,
        durationMs: null,
        reason: null,
      })),
    })

    for (const stage of manifest.stages) {
      if (this.cancelled) return this.fail(stage.name, 'Cancelled.', 'cancelled')
      const startedAt = this.now()
      this.patchStage(stage.name, { state: 'running', startedAt })
      const run = await this.exec(
        ['--stage', stage.name, '--json', '--non-interactive'],
        STAGE_TIMEOUT_MS,
        stage.name,
      )
      const durationMs = this.now() - startedAt
      if (this.cancelled) {
        this.patchStage(stage.name, { state: 'failed', durationMs, reason: 'Cancelled.' })
        return this.fail(stage.name, 'Cancelled.', 'cancelled')
      }
      const frame = lastJson(run.stdout)
      if (run.timedOut) {
        this.patchStage(stage.name, { state: 'failed', durationMs, reason: 'Timed out.' })
        return this.fail(
          stage.name,
          `"${stage.title}" took longer than ${STAGE_TIMEOUT_MS / 60000} minutes and was stopped.`,
        )
      }
      if (!frame || frame.stage !== stage.name) {
        this.patchStage(stage.name, { state: 'failed', durationMs, reason: 'No result.' })
        return this.fail(
          stage.name,
          `"${stage.title}" ended without a result (exit ${run.code ?? 'null'}).${tail(run.stderr)}`,
        )
      }
      if (frame.ok !== true) {
        const reason = typeof frame.reason === 'string' ? frame.reason : 'failed'
        this.patchStage(stage.name, { state: 'failed', durationMs, reason })
        return this.fail(stage.name, `"${stage.title}" failed: ${reason}${tail(run.stderr)}`)
      }
      this.patchStage(stage.name, {
        state: frame.skipped === true ? 'skipped' : 'succeeded',
        durationMs,
        reason: typeof frame.reason === 'string' ? frame.reason : null,
      })
    }

    this.closeLog()
    return this.set({ ...this.state, phase: 'succeeded', finishedAt: this.now() })
  }

  private async manifest(): Promise<
    { ok: true; stages: StageInfo[] } | { ok: false; error: string }
  > {
    const run = await this.exec(['--manifest'], MANIFEST_TIMEOUT_MS, null)
    const frame = lastJson(run.stdout)
    const raw = frame?.stages
    if (run.timedOut || run.code !== 0 || !Array.isArray(raw)) {
      return {
        ok: false,
        error: `Could not read the installer's stage list (exit ${run.code ?? 'null'}).${tail(run.stderr)}`,
      }
    }
    const stages: StageInfo[] = []
    for (const item of raw as unknown[]) {
      if (!item || typeof item !== 'object') continue
      const rec = item as Record<string, unknown>
      if (typeof rec.name !== 'string') continue
      stages.push({
        name: rec.name,
        title: typeof rec.title === 'string' ? rec.title : rec.name,
        category: typeof rec.category === 'string' ? rec.category : 'runtime',
        needsUserInput: rec.needs_user_input === true,
      })
    }
    if (stages.length === 0) return { ok: false, error: 'The installer reported no stages.' }
    return { ok: true, stages }
  }

  private exec(
    args: string[],
    timeoutMs: number,
    stage: string | null,
  ): Promise<{ code: number | null; timedOut: boolean; stdout: string; stderr: string }> {
    return new Promise((resolve) => {
      const version = this.deps.version.startsWith('v')
        ? this.deps.version
        : `v${this.deps.version}`
      const argv = [this.deps.scriptPath, '--version', version, ...args]
      let child: ChildProcess
      try {
        child = this.spawn('bash', argv, {
          env: hardenedEnv(),
          cwd: this.deps.cwd ?? path.dirname(this.deps.logDir),
          detached: process.platform !== 'win32',
          stdio: ['ignore', 'pipe', 'pipe'],
        })
      } catch (err) {
        resolve({ code: null, timedOut: false, stdout: '', stderr: String(err) })
        return
      }
      this.child = child
      let stdout = ''
      let stderr = ''
      let timedOut = false
      const out = lineSplitter((line) => this.appendLog({ stage, stream: 'stdout', line }))
      const err = lineSplitter((line) => this.appendLog({ stage, stream: 'stderr', line }))
      child.stdout?.on('data', (chunk: Buffer) => {
        const text = chunk.toString()
        stdout += text
        out(text)
      })
      child.stderr?.on('data', (chunk: Buffer) => {
        const text = chunk.toString()
        stderr += text
        err(text)
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
      child.once('error', (e) => {
        stderr += `\n${e.message}`
        finish(null)
      })
      child.once('exit', (code) => finish(code))
    })
  }

  private fail(
    stage: string | null,
    error: string,
    phase: 'failed' | 'cancelled' = 'failed',
  ): BootstrapState {
    this.appendLog({ stage, stream: 'stderr', line: error })
    this.closeLog()
    return this.set({ ...this.state, phase, error, finishedAt: this.now() })
  }

  private patchStage(name: string, patch: Partial<StageProgress>): void {
    this.set({
      ...this.state,
      stages: this.state.stages.map((s) => (s.name === name ? { ...s, ...patch } : s)),
    })
  }

  private appendLog(entry: LogLine): void {
    const line = entry.line.trimEnd()
    if (!line) return
    // The JSON result frame is protocol, not progress: keep it off screen.
    if (entry.stream === 'stdout' && line.startsWith('{')) return
    const record = { ...entry, line }
    this.logFile?.write(`${JSON.stringify({ t: this.now(), ...record })}\n`)
    this.set({ ...this.state, log: [...this.state.log, record].slice(-BOOTSTRAP_LOG_RING) })
  }

  private openLog(): void {
    this.closeLog()
    try {
      mkdirSync(this.deps.logDir, { recursive: true })
      const stamp = new Date(this.now()).toISOString().replace(/[:.]/g, '-')
      const file = path.join(this.deps.logDir, `bootstrap-${stamp}.log`)
      this.logFile = createWriteStream(file, { flags: 'a' })
      this.state = { ...this.state, logPath: file }
    } catch {
      this.logFile = null
    }
  }

  private closeLog(): void {
    this.logFile?.end()
    this.logFile = null
  }

  private set(next: BootstrapState): BootstrapState {
    this.state = next
    const copy = this.current()
    for (const fn of this.listeners) fn(copy)
    return copy
  }
}

/**
 * Where the bundled installer is. Packaged: `Contents/Resources/install.sh`
 * (electron-builder `extraResources`); in development the repo's own copy.
 */
export function bundledInstallScript(resourcesPath: string, repoRoot: string): string | null {
  for (const candidate of [
    path.join(resourcesPath, 'install.sh'),
    path.join(repoRoot, 'install.sh'),
  ]) {
    if (existsSync(candidate)) return candidate
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

function tail(stderr: string): string {
  const lines = stderr
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter(Boolean)
    .slice(-3)
  return lines.length ? `\n${lines.join('\n')}` : ''
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
