import { execFile, spawn as nodeSpawn, type ChildProcess } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { createWriteStream, mkdirSync, rmSync, writeFileSync, type WriteStream } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { gatewayUrl, type GatewaySettings } from '@shared/settings'
import { GATEWAY_EXIT_CONFIG, STOPPED_GATEWAY, type GatewayStatus } from '@shared/gateway'
import { hardenedEnv, lastJson } from '../updates/engine-updater'
import { locateCli } from './cli-locator'

type Listener = (status: GatewayStatus) => void

/** How long a freshly spawned gateway may take to answer /health. */
const READY_TIMEOUT_MS = 60_000
const READY_POLL_MS = 400
/**
 * Stop: SIGTERM starts the gateway's orderly shutdown (drain turns, stop
 * channels, tell clients). If it is still draining after STOP_DRAIN_MS, a
 * second SIGTERM makes it cancel the turns instead; SIGKILL only if that
 * does not finish within STOP_FORCE_MS either.
 */
export const STOP_DRAIN_MS = 8_000
export const STOP_FORCE_MS = 4_000
/** `agentos doctor --json` reads config offline; it should never take this long. */
const DOCTOR_TIMEOUT_MS = 30_000

/** Readiness of a spawned child: `/ready` answers, or an engine without it. */
export type ReadyProbe = (url: string) => Promise<'ready' | 'waiting' | 'unsupported'>

/** Runs `<cli> doctor --json` and returns its one-line summary, or null. Never throws. */
export type DoctorRunner = (cli: string) => Promise<string | null>

/** How one spawn ended before the supervisor could call it running. */
type LaunchOutcome =
  | { kind: 'ready' }
  | { kind: 'timeout' }
  | { kind: 'stopped' }
  | { kind: 'superseded' }
  | { kind: 'spawn-error'; message: string }
  | { kind: 'exit'; code: number | null; signal: string | null; stderrTail: string }

/**
 * Owns the lifecycle of the local gateway process when settings.mode is
 * `managed`. In `external` mode it only checks that the configured endpoint
 * answers and never spawns anything.
 *
 * `running` is only reported for a spawned child once `GET /ready` answers
 * 200 (the gateway refuses WebSocket connects with a retryable error while
 * its boot sidecars start), so the renderer can connect the moment it sees
 * that state without its own retry dance. An engine without `/ready` falls
 * back to `GET /health`. A child that exits 78 (configuration error) gets
 * one `agentos doctor` run and one retry before the error is surfaced.
 *
 * Deliberately thin: the gateway itself owns pid-locking, port binding and
 * auth (see src/agentos/gateway). The shell just starts, watches and stops it.
 */
export class GatewaySupervisor {
  private child: ChildProcess | null = null
  private status: GatewayStatus = { ...STOPPED_GATEWAY }
  private readonly listeners = new Set<Listener>()
  private startGeneration = 0

  private readonly probe: (url: string) => Promise<boolean>
  private readonly probeReady: ReadyProbe
  private readonly runDoctor: DoctorRunner
  private readonly locate: (override: string | null) => string | null

  constructor(
    private readonly getSettings: () => GatewaySettings,
    deps: {
      probe?: (url: string) => Promise<boolean>
      /**
       * Readiness of a child this app spawned. Defaults to `GET /ready`; when
       * only `probe` is injected (tests), that liveness probe stands in.
       */
      probeReady?: ReadyProbe
      /** Diagnoses an exit-78 child before the one retry. */
      runDoctor?: DoctorRunner
      locate?: (override: string | null) => string | null
      /** Where the spawned gateway's stdout/stderr are appended (~/Library/Logs/AgentOS). */
      logPath?: string
      /** Where the one-shot operator secret file is written before a spawn. */
      secretDir?: string
      spawn?: typeof nodeSpawn
    } = {},
  ) {
    this.probe = deps.probe ?? defaultProbe
    this.probeReady =
      deps.probeReady ??
      (deps.probe
        ? async (url) => ((await deps.probe!(url)) ? 'ready' : 'waiting')
        : defaultReadyProbe)
    this.runDoctor = deps.runDoctor ?? defaultDoctorRunner
    this.locate = deps.locate ?? ((override) => locateCli({ override }))
    this.logPath = deps.logPath ?? null
    this.secretDir = deps.secretDir ?? os.tmpdir()
    this.spawn = deps.spawn ?? nodeSpawn
  }

  private readonly spawn: typeof nodeSpawn

  private readonly logPath: string | null
  private logFile: WriteStream | null = null

  private readonly secretDir: string
  /**
   * The operator secret of the gateway this app spawned. The gateway reads it
   * from a 0600 file at boot and deletes the file, so no child of the gateway
   * (an agent's shell) can find it; the renderer presents it at the WebSocket
   * handshake and the gateway then knows the app's connection is the user's,
   * not an agent's (src/agentos/gateway/agent_surface.py). Only in memory
   * here; never logged, never in settings.
   */
  private secret: string | null = null

  /** Null for an adopted or external gateway: those were not ours to brief. */
  operatorSecret(): string | null {
    return this.child && this.status.state !== 'stopped' ? this.secret : null
  }

  private writeSecretFile(): string | null {
    this.secret = randomBytes(32).toString('hex')
    const file = path.join(this.secretDir, `agentos-operator-${process.pid}-${Date.now()}.secret`)
    try {
      mkdirSync(this.secretDir, { recursive: true })
      writeFileSync(file, this.secret + '\n', { mode: 0o600 })
      return file
    } catch {
      // Without the file the gateway falls back to its exec-window rule and
      // the app still works; the connection just cannot prove it is ours.
      this.secret = null
      return null
    }
  }

  private discardSecretFile(file: string | null): void {
    if (!file) return
    try {
      rmSync(file, { force: true })
    } catch {
      /* the gateway already deleted it */
    }
  }

  /**
   * Every line the gateway prints goes to disk, or nobody can answer "why
   * did the titler not rename that session" on a Mac that only ever ran the
   * app. Appended, with a header per spawn; the supervisor keeps no more
   * than a stderr tail in memory.
   */
  private openLog(cli: string): void {
    this.closeLog()
    if (!this.logPath) return
    try {
      mkdirSync(path.dirname(this.logPath), { recursive: true })
      this.logFile = createWriteStream(this.logPath, { flags: 'a' })
      this.logFile.write(`\n===== ${new Date().toISOString()} gateway run (${cli}) =====\n`)
    } catch {
      this.logFile = null
    }
  }

  private closeLog(): void {
    this.logFile?.end()
    this.logFile = null
  }

  current(): GatewayStatus {
    return { ...this.status }
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  async start(): Promise<GatewayStatus> {
    const cfg = this.getSettings()
    const url = gatewayUrl(cfg)
    const generation = ++this.startGeneration

    if (cfg.mode === 'external') {
      this.set(status({ state: 'starting', url }))
      const ok = await this.waitReady(url, 5_000, () => generation === this.startGeneration)
      return this.set(
        ok
          ? status({ state: 'running', url })
          : status({ state: 'error', error: `No gateway answering at ${url}.` }),
      )
    }

    if (this.child && (this.status.state === 'running' || this.status.state === 'starting')) {
      return this.current()
    }

    // A gateway may already be up from a terminal: adopt it instead of
    // fighting over the port. The gateway's own pidlock would reject us anyway.
    if (await this.probe(url)) {
      return this.set(status({ state: 'running', url }))
    }

    const cli = this.locate(cfg.cliPath)
    if (!cli) {
      return this.set(
        status({
          state: 'error',
          error: 'agentos CLI not found. Install it or set gateway.cliPath in settings.',
        }),
      )
    }

    const stillWanted = (): boolean => generation === this.startGeneration
    this.set(status({ state: 'starting', url }))
    let outcome = await this.launch(cli, cfg, url, stillWanted)

    // Exit 78 is the gateway saying "only a config fix helps". Ask doctor
    // what is wrong (so the error can say it), then give it exactly one more
    // try: the operator may have fixed the file meanwhile, or the failure was
    // a migration that a second boot gets past.
    let doctorSummary: string | null = null
    if (outcome.kind === 'exit' && outcome.code === GATEWAY_EXIT_CONFIG && stillWanted()) {
      this.set(
        status({
          state: 'starting',
          url,
          exitCode: outcome.code,
          detail: `Configuration error (exit ${GATEWAY_EXIT_CONFIG}): running agentos doctor`,
        }),
      )
      doctorSummary = await this.runDoctor(cli)
      if (!stillWanted()) return this.current()
      this.set(status({ state: 'starting', url, exitCode: outcome.code, detail: 'Retrying start' }))
      outcome = await this.launch(cli, cfg, url, stillWanted)
    }

    switch (outcome.kind) {
      case 'ready':
        return this.set(status({ state: 'running', pid: this.child?.pid ?? null, url }))
      case 'stopped':
      case 'superseded':
        return this.current()
      case 'spawn-error':
        return this.set(status({ state: 'error', error: outcome.message }))
      case 'timeout':
        return this.set(
          status({
            state: 'error',
            error: `Gateway did not become healthy within ${READY_TIMEOUT_MS / 1000}s.`,
          }),
        )
      case 'exit':
        return this.set(
          status({
            state: 'error',
            exitCode: outcome.code,
            error: exitMessage(outcome, doctorSummary),
          }),
        )
    }
  }

  /**
   * Spawn the gateway once and wait until it is ready, exits, or times out.
   * An exit after this returns (a crash while running) is reported by the
   * exit listener itself, as before.
   */
  private async launch(
    cli: string,
    cfg: GatewaySettings,
    url: string,
    stillWanted: () => boolean,
  ): Promise<LaunchOutcome> {
    // Host and port go on the command line: `gateway run --bind/--port` beat
    // config.toml, whereas an environment variable loses to a `port =` line
    // the onboarding wrote (verified against GatewayConfig.load). The auth
    // env names follow AuthConfig's prefix, `AGENTOS_AUTH_<FIELD>`. A GUI
    // launch hands us a bare PATH; without the login-shell dirs the gateway's
    // harness checks miss Homebrew/npm CLIs such as `codex` and `npx`.
    const secretFile = this.writeSecretFile()
    const child = this.spawn(
      cli,
      ['gateway', 'run', '--bind', cfg.host, '--port', String(cfg.port)],
      {
        env: {
          ...hardenedEnv(),
          ...(cfg.token ? { AGENTOS_AUTH_TOKEN: cfg.token, AGENTOS_AUTH_MODE: 'token' } : {}),
          ...(secretFile ? { AGENTOS_OPERATOR_SECRET_FILE: secretFile } : {}),
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      },
    )
    this.child = child
    this.openLog(cli)
    let stderrTail = ''
    child.stdout?.on('data', (chunk: Buffer) => {
      this.logFile?.write(chunk)
    })
    child.stderr?.on('data', (chunk: Buffer) => {
      this.logFile?.write(chunk)
      stderrTail = (stderrTail + chunk.toString()).slice(-2000)
    })

    // While this launch is still waiting, an exit or spawn error is its
    // outcome; once it has returned, the listeners report on their own.
    let launching = true
    let early = null as LaunchOutcome | null
    child.once('error', (err) => {
      if (launching) {
        early = { kind: 'spawn-error', message: err.message }
        if (this.child === child) this.child = null
        return
      }
      this.set(status({ state: 'error', error: err.message }))
    })
    child.once('exit', (code, signal) => {
      if (this.child === child) this.child = null
      this.logFile?.write(`===== exit code=${code ?? 'null'} signal=${signal ?? 'null'} =====\n`)
      this.closeLog()
      const clean = this.status.state === 'stopping' || code === 0
      if (launching) {
        if (clean) {
          this.set({ ...STOPPED_GATEWAY, exitCode: code })
          early = { kind: 'stopped' }
        } else {
          early = { kind: 'exit', code, signal, stderrTail }
        }
        return
      }
      this.set(
        clean
          ? { ...STOPPED_GATEWAY, exitCode: code }
          : status({
              state: 'error',
              exitCode: code,
              error: exitMessage({ code, signal, stderrTail }, null),
            }),
      )
    })

    const ready = await this.waitChildReady(
      url,
      READY_TIMEOUT_MS,
      () => stillWanted() && this.child === child && early === null,
    )
    launching = false
    // The gateway deletes the file at boot; if it never got that far, do not
    // leave a secret lying in the temp dir.
    this.discardSecretFile(secretFile)
    if (early !== null) return early
    if (!stillWanted() || this.child !== child) return { kind: 'superseded' }
    if (!ready) {
      child.kill('SIGTERM')
      return { kind: 'timeout' }
    }
    return { kind: 'ready' }
  }

  /**
   * Adopt a gateway that already answers on the configured endpoint, without
   * ever spawning one. Returns null when nothing is listening. Used at launch
   * before the engine install is offered: a running gateway (a terminal, a
   * previous app) is usable as-is, and installing over it would be wrong.
   */
  async adopt(): Promise<GatewayStatus | null> {
    const cfg = this.getSettings()
    const url = gatewayUrl(cfg)
    if (this.child && (this.status.state === 'running' || this.status.state === 'starting')) {
      return this.current()
    }
    if (!(await this.probe(url))) return null
    this.startGeneration++
    return this.set(status({ state: 'running', url }))
  }

  /**
   * Stop, then start again reading settings afresh, so a changed endpoint,
   * mode or CLI path takes effect. An adopted (external) gateway is not ours
   * to kill: restart then just re-probes the configured endpoint.
   */
  async restart(): Promise<GatewayStatus> {
    await this.stop()
    return this.start()
  }

  /**
   * Settings saved with a different run mode. Switching to `external` means
   * the app no longer runs a gateway, so a child it spawned is stopped
   * rather than left behind, and the configured endpoint is probed in its
   * place. An adopted gateway was never ours and stays up. Switching to
   * `managed` is the user asking the app to run the gateway: the status
   * still points at the external endpoint, so it is dropped and the local
   * gateway started (or adopted, if one already answers there).
   */
  async modeChanged(
    previous: GatewaySettings['mode'],
    next: GatewaySettings['mode'],
  ): Promise<void> {
    if (previous === next) return
    if (next === 'external' && !this.child) return
    await this.restart()
  }

  async stop(): Promise<GatewayStatus> {
    this.startGeneration++
    const child = this.child
    if (!child) {
      return this.set({ ...STOPPED_GATEWAY })
    }
    this.set({ ...this.status, state: 'stopping' })
    await new Promise<void>((resolve) => {
      let killTimer: ReturnType<typeof setTimeout> | null = null
      const forceTimer = setTimeout(() => {
        child.kill('SIGTERM')
        killTimer = setTimeout(() => {
          child.kill('SIGKILL')
          resolve()
        }, STOP_FORCE_MS)
      }, STOP_DRAIN_MS)
      child.once('exit', () => {
        clearTimeout(forceTimer)
        if (killTimer) clearTimeout(killTimer)
        resolve()
      })
      child.kill('SIGTERM')
    })
    return this.current()
  }

  private async waitReady(
    url: string,
    timeoutMs: number,
    stillWanted: () => boolean,
  ): Promise<boolean> {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline && stillWanted()) {
      if (await this.probe(url)) return true
      await new Promise((r) => setTimeout(r, READY_POLL_MS))
    }
    return false
  }

  /** Like `waitReady`, for a spawned child: `/ready`, else `/health` on an older engine. */
  private async waitChildReady(
    url: string,
    timeoutMs: number,
    stillWanted: () => boolean,
  ): Promise<boolean> {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline && stillWanted()) {
      const ready = await this.probeReady(url)
      if (ready === 'ready') return true
      if (ready === 'unsupported' && (await this.probe(url))) return true
      if (!stillWanted()) return false
      await new Promise((r) => setTimeout(r, READY_POLL_MS))
    }
    return false
  }

  private set(next: GatewayStatus): GatewayStatus {
    this.status = next
    for (const fn of this.listeners) fn({ ...next })
    return { ...next }
  }
}

/** A full status with the optional fields filled, so every emitted status has them all. */
function status(fields: Partial<GatewayStatus> & Pick<GatewayStatus, 'state'>): GatewayStatus {
  return { ...STOPPED_GATEWAY, ...fields }
}

function exitMessage(
  exit: { code: number | null; signal: string | null; stderrTail: string },
  doctorSummary: string | null,
): string {
  let message = `gateway exited (code=${exit.code ?? 'null'}, signal=${exit.signal ?? 'null'})`
  if (exit.code === GATEWAY_EXIT_CONFIG) {
    message = `gateway exited with a configuration error (exit ${GATEWAY_EXIT_CONFIG})`
    message += doctorSummary
      ? `\nagentos doctor: ${doctorSummary}`
      : '\nRun `agentos doctor` for details.'
  }
  const tail = exit.stderrTail.trim()
  return tail ? `${message}\n${tail.split('\n').slice(-3).join('\n')}` : message
}

/**
 * One line from an `agentos doctor --json` report: the first error finding
 * that is not "the gateway is not running" (we know), plus its first command.
 */
export function summarizeDoctorReport(report: Record<string, unknown> | null): string | null {
  const findings = Array.isArray(report?.findings) ? report.findings : []
  for (const raw of findings) {
    if (!raw || typeof raw !== 'object') continue
    const finding = raw as Record<string, unknown>
    if (finding.severity !== 'error') continue
    if (String(finding.id ?? '').startsWith('gateway.')) continue
    const title = String(finding.title ?? '').trim()
    if (!title) continue
    const steps = Array.isArray(finding.fixSteps) ? finding.fixSteps : []
    const command = steps
      .map((step) =>
        step && typeof step === 'object' ? (step as { command?: unknown }).command : null,
      )
      .find((value): value is string => typeof value === 'string' && value.trim() !== '')
    return command ? `${title} (fix: ${command})` : title
  }
  return null
}

function defaultDoctorRunner(cli: string): Promise<string | null> {
  return new Promise((resolve) => {
    try {
      execFile(
        cli,
        ['doctor', '--json'],
        { env: hardenedEnv(), timeout: DOCTOR_TIMEOUT_MS, maxBuffer: 4 * 1024 * 1024 },
        // doctor exits 1 when it finds something; the report is on stdout either way.
        (_error, stdout) => resolve(summarizeDoctorReport(lastJson(String(stdout ?? '')))),
      )
    } catch {
      resolve(null)
    }
  })
}

async function defaultReadyProbe(url: string): Promise<'ready' | 'waiting' | 'unsupported'> {
  try {
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), 1500)
    const res = await fetch(`${url}/ready`, { signal: ctl.signal })
    clearTimeout(timer)
    if (res.ok) return 'ready'
    return res.status === 404 ? 'unsupported' : 'waiting'
  } catch {
    return 'waiting'
  }
}

async function defaultProbe(url: string): Promise<boolean> {
  try {
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), 1500)
    const res = await fetch(`${url}/health`, { signal: ctl.signal })
    clearTimeout(timer)
    return res.ok
  } catch {
    return false
  }
}
