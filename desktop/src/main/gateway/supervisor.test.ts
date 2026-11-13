// @vitest-environment node
import { EventEmitter } from 'node:events'
import { existsSync, mkdtempSync, readFileSync, statSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { PassThrough } from 'node:stream'
import { describe, expect, it, vi } from 'vitest'
import type { GatewaySettings } from '@shared/settings'
import {
  GatewaySupervisor,
  STOP_DRAIN_MS,
  STOP_FORCE_MS,
  summarizeDoctorReport,
} from './supervisor'

const external: GatewaySettings = {
  mode: 'external',
  scheme: 'http',
  host: '127.0.0.1',
  port: 18791,
  token: null,
  cliPath: null,
}

describe('GatewaySupervisor (external mode)', () => {
  it('reports running once the endpoint answers /health', async () => {
    const probe = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    const sup = new GatewaySupervisor(() => external, { probe })
    const seen: string[] = []
    sup.subscribe((s) => seen.push(s.state))
    const status = await sup.start()
    expect(status).toMatchObject({ state: 'running', url: 'http://127.0.0.1:18791', pid: null })
    expect(seen).toEqual(['starting', 'running'])
    expect(probe).toHaveBeenCalledWith('http://127.0.0.1:18791')
  })

  it('probes an https gateway on another host over https', async () => {
    const probe = vi.fn().mockResolvedValue(true)
    const remote: GatewaySettings = {
      ...external,
      scheme: 'https',
      host: 'gw.example.com',
      port: 443,
    }
    const sup = new GatewaySupervisor(() => remote, { probe })
    const status = await sup.start()
    expect(status).toMatchObject({ state: 'running', url: 'https://gw.example.com:443' })
    expect(probe).toHaveBeenCalledWith('https://gw.example.com:443')
    expect(await sup.adopt()).toMatchObject({ url: 'https://gw.example.com:443' })
  })

  it('reports an error when nothing answers', async () => {
    vi.useFakeTimers()
    const sup = new GatewaySupervisor(() => external, { probe: async () => false })
    const pending = sup.start()
    await vi.advanceTimersByTimeAsync(6_000)
    const status = await pending
    vi.useRealTimers()
    expect(status.state).toBe('error')
    expect(status.error).toContain('No gateway answering')
  })

  it('stop() never spawns and resets to stopped', async () => {
    const sup = new GatewaySupervisor(() => external, { probe: async () => true })
    await sup.start()
    expect((await sup.stop()).state).toBe('stopped')
  })
})

describe('GatewaySupervisor (managed mode)', () => {
  it('adopts a gateway that is already up instead of spawning', async () => {
    const managed: GatewaySettings = { ...external, mode: 'managed' }
    const sup = new GatewaySupervisor(() => managed, { probe: async () => true })
    const status = await sup.start()
    expect(status).toMatchObject({ state: 'running', pid: null })
  })

  it('errors out when the CLI cannot be found', async () => {
    const managed: GatewaySettings = { ...external, mode: 'managed' }
    const sup = new GatewaySupervisor(() => managed, {
      probe: async () => false,
      locate: () => null,
    })
    const status = await sup.start()
    expect(status.state).toBe('error')
    expect(status.error).toContain('agentos CLI not found')
  })
})

describe('GatewaySupervisor.adopt', () => {
  const managed: GatewaySettings = { ...external, mode: 'managed' }

  it('reports a listening gateway without spawning', async () => {
    const sup = new GatewaySupervisor(() => managed, {
      probe: async () => true,
      locate: () => null,
    })
    const status = await sup.adopt()
    expect(status).toMatchObject({ state: 'running', pid: null, url: 'http://127.0.0.1:18791' })
    expect(sup.current().state).toBe('running')
  })

  it('returns null and stays stopped when nothing answers', async () => {
    const sup = new GatewaySupervisor(() => managed, {
      probe: async () => false,
      locate: () => null,
    })
    expect(await sup.adopt()).toBeNull()
    expect(sup.current().state).toBe('stopped')
  })
})

/** A spawn stand-in that records each call and hands back a quiet child. */
function fakeSpawn() {
  const spawnCalls: { command: string; args: string[]; env: NodeJS.ProcessEnv }[] = []
  const spawn = vi.fn((command: string, args: string[], options: { env: NodeJS.ProcessEnv }) => {
    spawnCalls.push({ command, args, env: options.env })
    const child = new EventEmitter() as EventEmitter & {
      pid: number
      stdout: PassThrough
      stderr: PassThrough
      kill: () => boolean
    }
    child.pid = 4242
    child.stdout = new PassThrough()
    child.stderr = new PassThrough()
    child.kill = () => true
    return child
  })
  return { spawn: spawn as unknown as typeof import('node:child_process').spawn, spawnCalls }
}

describe('GatewaySupervisor (spawn arguments)', () => {
  it('binds host and port on the command line and passes the token through AuthConfig env', async () => {
    const managed: GatewaySettings = {
      mode: 'managed',
      scheme: 'http',
      host: '127.0.0.1',
      port: 18999,
      token: 'secret',
      cliPath: '/opt/agentos',
    }
    const { spawn, spawnCalls } = fakeSpawn()
    let probes = 0
    const secretDir = mkdtempSync(path.join(os.tmpdir(), 'agentos-sup-'))
    let seenSecret: string | null = null
    let seenMode: number | null = null
    const sup = new GatewaySupervisor(() => managed, {
      // Not running before the spawn; healthy right after it. The probe is
      // where the "gateway" reads its secret, as the real one does at boot.
      probe: async (url) => {
        const file = spawnCalls[0]?.env.AGENTOS_OPERATOR_SECRET_FILE
        if (file && seenSecret === null) {
          seenSecret = readFileSync(file, 'utf8').trim()
          seenMode = statSync(file).mode
        }
        return probes++ > 0 && url.length > 0
      },
      locate: (override) => override,
      secretDir,
      spawn,
    })
    const status = await sup.start()
    expect(status).toMatchObject({ state: 'running', pid: 4242, url: 'http://127.0.0.1:18999' })
    expect(spawnCalls[0]?.command).toBe('/opt/agentos')
    expect(spawnCalls[0]?.args).toEqual([
      'gateway',
      'run',
      '--bind',
      '127.0.0.1',
      '--port',
      '18999',
    ])
    expect(spawnCalls[0]?.env.AGENTOS_AUTH_TOKEN).toBe('secret')
    expect(spawnCalls[0]?.env.AGENTOS_AUTH_MODE).toBe('token')
    // The operator secret travelled by file, 0600, and is what the renderer
    // will present; the file is gone once the gateway is up.
    const secretFile = spawnCalls[0]?.env.AGENTOS_OPERATOR_SECRET_FILE
    expect(secretFile).toBeTruthy()
    expect(secretFile).toContain(secretDir)
    expect(seenSecret).toMatch(/^[0-9a-f]{64}$/)
    expect(seenMode! & 0o777).toBe(0o600)
    expect(sup.operatorSecret()).toBe(seenSecret)
    expect(existsSync(secretFile!)).toBe(false)
    expect(spawnCalls[0]?.env.AGENTOS_OPERATOR_SECRET).toBeUndefined()
    // The old double-underscore names never reached the gateway's settings.
    expect(spawnCalls[0]?.env.AGENTOS_GATEWAY__PORT).toBeUndefined()
    expect(spawnCalls[0]?.env.AGENTOS_AUTH__TOKEN).toBeUndefined()
  })

  it('spawns the gateway with the login-shell dirs on PATH, not the bare GUI PATH', async () => {
    // Finder/Dock launches get PATH=/usr/bin:/bin:/usr/sbin:/sbin; the gateway
    // resolves ACP harness CLIs (codex, npx) with which() against this PATH.
    vi.stubEnv('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')
    const { spawn, spawnCalls } = fakeSpawn()
    let probes = 0
    const sup = new GatewaySupervisor(
      () => ({ ...external, mode: 'managed', cliPath: '/opt/agentos' }),
      {
        probe: async () => probes++ > 0,
        locate: (override) => override,
        secretDir: mkdtempSync(path.join(os.tmpdir(), 'agentos-sup-')),
        spawn,
      },
    )
    try {
      await sup.start()
    } finally {
      vi.unstubAllEnvs()
    }
    const entries = (spawnCalls[0]?.env.PATH ?? '').split(path.delimiter)
    expect(entries.slice(0, 4)).toEqual(['/usr/bin', '/bin', '/usr/sbin', '/sbin'])
    expect(entries).toContain('/opt/homebrew/bin')
    expect(entries).toContain('/usr/local/bin')
    expect(entries).toContain(path.join(os.homedir(), '.local', 'bin'))
  })

  it('has no operator secret to offer for an adopted or external gateway', async () => {
    const sup = new GatewaySupervisor(() => external, { probe: async () => true })
    await sup.start()
    expect(sup.operatorSecret()).toBeNull()
  })
})

describe('GatewaySupervisor.stop escalation', () => {
  async function startedWithChild(onKill: (signal: string) => void) {
    const child = new EventEmitter() as EventEmitter & {
      pid: number
      stdout: PassThrough
      stderr: PassThrough
      kill: (signal: string) => boolean
    }
    child.pid = 4343
    child.stdout = new PassThrough()
    child.stderr = new PassThrough()
    child.kill = (signal: string) => {
      onKill(signal)
      return true
    }
    const spawn = vi.fn(() => child) as unknown as typeof import('node:child_process').spawn
    let probes = 0
    const sup = new GatewaySupervisor(
      () => ({ ...external, mode: 'managed', cliPath: '/opt/agentos' }),
      {
        probe: async () => probes++ > 0,
        locate: (override) => override,
        secretDir: mkdtempSync(path.join(os.tmpdir(), 'agentos-sup-')),
        spawn,
      },
    )
    await sup.start()
    return { sup, child }
  }

  it('lets the gateway drain, then forces with a second SIGTERM, then SIGKILL', async () => {
    const signals: string[] = []
    const { sup } = await startedWithChild((signal) => signals.push(signal))
    vi.useFakeTimers()
    try {
      const stopping = sup.stop()
      expect(signals).toEqual(['SIGTERM'])
      await vi.advanceTimersByTimeAsync(STOP_DRAIN_MS)
      expect(signals).toEqual(['SIGTERM', 'SIGTERM'])
      await vi.advanceTimersByTimeAsync(STOP_FORCE_MS)
      expect(signals).toEqual(['SIGTERM', 'SIGTERM', 'SIGKILL'])
      await stopping
    } finally {
      vi.useRealTimers()
    }
  })

  it('stops the managed child when the run mode turns external, then probes the endpoint', async () => {
    const signals: string[] = []
    let exit: (() => void) | null = null
    const { sup, child } = await startedWithChild((signal) => {
      signals.push(signal)
      exit?.()
    })
    exit = () => queueMicrotask(() => child.emit('exit', 0, null))
    expect(sup.current()).toMatchObject({ state: 'running', pid: 4343 })

    await sup.modeChanged('managed', 'external')
    expect(signals).toEqual(['SIGTERM'])
    // The probe answers (the stand-in's counter is past zero), so the
    // supervisor now reports the external gateway it re-probed, not a child.
    expect(sup.current()).toMatchObject({ state: 'running', pid: null })

    // Already external: nothing to stop, nothing to signal.
    await sup.modeChanged('external', 'external')
    expect(signals).toEqual(['SIGTERM'])
  })

  it('drops the external endpoint and brings up the local gateway when the mode turns managed', async () => {
    let cfg: GatewaySettings = {
      ...external,
      scheme: 'https',
      host: 'gw.example.com',
      port: 443,
    }
    let adopted = false
    const probe = vi.fn(async (url: string) => url !== 'http://127.0.0.1:18791' || adopted)
    const sup = new GatewaySupervisor(() => cfg, { probe, locate: () => null })
    await sup.start()
    expect(sup.current()).toMatchObject({ state: 'running', url: 'https://gw.example.com:443' })

    // Saved as managed on the local default; a gateway already answers there.
    cfg = { ...external, mode: 'managed' }
    adopted = true
    await sup.modeChanged('external', 'managed')
    expect(sup.current()).toMatchObject({
      state: 'running',
      url: 'http://127.0.0.1:18791',
      pid: null,
    })
  })

  it('reports why the local gateway did not come up when the mode turns managed', async () => {
    let cfg: GatewaySettings = { ...external, host: 'gw.example.com' }
    const sup = new GatewaySupervisor(() => cfg, {
      probe: async (url) => url.includes('gw.example.com'),
      locate: () => null,
    })
    await sup.start()
    cfg = { ...external, mode: 'managed' }
    await sup.modeChanged('external', 'managed')
    // Not left showing the external gateway as running.
    expect(sup.current()).toMatchObject({ state: 'error', url: null })
    expect(sup.current().error).toMatch(/CLI not found/)
  })

  it('leaves an adopted gateway alone when the mode turns external', async () => {
    const sup = new GatewaySupervisor(() => ({ ...external, mode: 'managed' }), {
      probe: async () => true,
      locate: () => null,
    })
    await sup.adopt()
    await sup.modeChanged('managed', 'external')
    expect(sup.current()).toMatchObject({ state: 'running', pid: null })
  })

  it('sends nothing more once the gateway exits on the first SIGTERM', async () => {
    const signals: string[] = []
    let exit: (() => void) | null = null
    const { sup, child } = await startedWithChild((signal) => {
      signals.push(signal)
      exit?.()
    })
    exit = () => queueMicrotask(() => child.emit('exit', 0, null))
    vi.useFakeTimers()
    try {
      const stopping = sup.stop()
      await vi.advanceTimersByTimeAsync(STOP_DRAIN_MS + STOP_FORCE_MS)
      await stopping
      expect(signals).toEqual(['SIGTERM'])
    } finally {
      vi.useRealTimers()
    }
  })
})

/**
 * A spawn stand-in whose children the test drives: each spawned child runs
 * `script(child, index)` right after the supervisor has attached listeners.
 */
function scriptedSpawn(
  script: (child: EventEmitter & { stderr: PassThrough }, index: number) => void,
) {
  let count = 0
  const spawn = vi.fn(() => {
    const index = count++
    const child = new EventEmitter() as EventEmitter & {
      pid: number
      stdout: PassThrough
      stderr: PassThrough
      kill: () => boolean
    }
    child.pid = 5000 + index
    child.stdout = new PassThrough()
    child.stderr = new PassThrough()
    child.kill = () => true
    setTimeout(() => script(child, index), 5)
    return child
  })
  return {
    spawn: spawn as unknown as typeof import('node:child_process').spawn,
    spawned: () => count,
  }
}

const managedCli: GatewaySettings = {
  mode: 'managed',
  scheme: 'http',
  host: '127.0.0.1',
  port: 18998,
  token: null,
  cliPath: '/opt/agentos',
}

function managedDeps(extra: Partial<ConstructorParameters<typeof GatewaySupervisor>[1]>) {
  return {
    locate: (override: string | null) => override,
    secretDir: mkdtempSync(path.join(os.tmpdir(), 'agentos-sup-')),
    ...extra,
  }
}

describe('GatewaySupervisor (exit 78: configuration error)', () => {
  it('runs doctor once, retries once, and reports running when the retry boots', async () => {
    const { spawn, spawned } = scriptedSpawn((child, index) => {
      if (index === 0) child.emit('exit', 78, null)
    })
    const runDoctor = vi.fn(async () => 'Local configuration cannot be loaded')
    const sup = new GatewaySupervisor(
      () => managedCli,
      managedDeps({
        probe: async () => false,
        probeReady: async () => (spawned() >= 2 ? 'ready' : 'waiting'),
        runDoctor,
        spawn,
      }),
    )
    const seen: { state: string; detail: string | null | undefined }[] = []
    sup.subscribe((s) => seen.push({ state: s.state, detail: s.detail }))

    const status = await sup.start()

    expect(status).toMatchObject({ state: 'running', pid: 5001, error: null, detail: null })
    expect(spawned()).toBe(2)
    expect(runDoctor).toHaveBeenCalledTimes(1)
    expect(runDoctor).toHaveBeenCalledWith('/opt/agentos')
    expect(seen).toEqual([
      { state: 'starting', detail: null },
      { state: 'starting', detail: 'Configuration error (exit 78): running agentos doctor' },
      { state: 'starting', detail: 'Retrying start' },
      { state: 'running', detail: null },
    ])
  })

  it('surfaces the exit code and the doctor summary when the retry fails too', async () => {
    const { spawn, spawned } = scriptedSpawn((child) => {
      child.stderr.write('AgentOS config error: the gateway configuration is invalid\n')
      setTimeout(() => child.emit('exit', 78, null), 5)
    })
    const runDoctor = vi.fn(
      async () => 'Local configuration cannot be loaded (fix: agentos onboard status --json)',
    )
    const sup = new GatewaySupervisor(
      () => managedCli,
      managedDeps({
        probe: async () => false,
        probeReady: async () => 'waiting',
        runDoctor,
        spawn,
      }),
    )

    const status = await sup.start()

    expect(spawned()).toBe(2)
    expect(runDoctor).toHaveBeenCalledTimes(1)
    expect(status.state).toBe('error')
    expect(status.exitCode).toBe(78)
    expect(status.error).toContain('configuration error (exit 78)')
    expect(status.error).toContain(
      'agentos doctor: Local configuration cannot be loaded (fix: agentos onboard status --json)',
    )
    expect(status.error).toContain('AgentOS config error')
  })

  it('keeps the old behaviour for any other exit code: no doctor, no retry', async () => {
    const { spawn, spawned } = scriptedSpawn((child) => {
      child.stderr.write('Traceback: boom\n')
      setTimeout(() => child.emit('exit', 1, null), 5)
    })
    const runDoctor = vi.fn(async () => 'never')
    const sup = new GatewaySupervisor(
      () => managedCli,
      managedDeps({
        probe: async () => false,
        probeReady: async () => 'waiting',
        runDoctor,
        spawn,
      }),
    )

    const status = await sup.start()

    expect(spawned()).toBe(1)
    expect(runDoctor).not.toHaveBeenCalled()
    expect(status).toMatchObject({ state: 'error', exitCode: 1 })
    expect(status.error).toContain('gateway exited (code=1, signal=null)')
    expect(status.error).toContain('Traceback: boom')
  })

  it('reports stopped, not error, when stop() lands during the doctor run', async () => {
    const { spawn, spawned } = scriptedSpawn((child) => child.emit('exit', 78, null))
    let sup: GatewaySupervisor | null = null
    const runDoctor = vi.fn(async () => {
      await sup!.stop()
      return null
    })
    sup = new GatewaySupervisor(
      () => managedCli,
      managedDeps({
        probe: async () => false,
        probeReady: async () => 'waiting',
        runDoctor,
        spawn,
      }),
    )

    const status = await sup.start()

    expect(spawned()).toBe(1)
    expect(status.state).toBe('stopped')
  })
})

describe('GatewaySupervisor (readiness of a spawned child)', () => {
  /** fetch stand-in: nothing listens before the spawn, then `/ready` follows `ready`. */
  function stubGateway(ready: number[], spawned: () => number) {
    const calls: string[] = []
    const fetchMock = vi.fn(async (input: string | URL) => {
      const url = String(input)
      calls.push(url)
      if (spawned() === 0) throw new Error('ECONNREFUSED')
      if (url.endsWith('/ready')) {
        const code = ready.length > 1 ? ready.shift()! : ready[0]!
        return new Response('{}', { status: code })
      }
      return new Response('{"ok":true}', { status: 200 })
    })
    vi.stubGlobal('fetch', fetchMock)
    return calls
  }

  it('waits for /ready to answer 200; a 503 keeps it starting', async () => {
    const { spawn, spawned } = scriptedSpawn(() => {})
    const calls = stubGateway([503, 503, 200], spawned)
    const sup = new GatewaySupervisor(() => managedCli, managedDeps({ spawn }))
    const seen: string[] = []
    sup.subscribe((s) => seen.push(s.state))
    try {
      const status = await sup.start()
      expect(status).toMatchObject({ state: 'running', pid: 5000 })
    } finally {
      vi.unstubAllGlobals()
    }
    const readyCalls = calls.filter((url) => url.endsWith('/ready'))
    expect(readyCalls).toHaveLength(3)
    // Only the pre-spawn adoption check used /health.
    expect(calls.filter((url) => url.endsWith('/health'))).toHaveLength(1)
    expect(seen).toEqual(['starting', 'running'])
  })

  it('falls back to /health on an engine whose /ready is a 404', async () => {
    const { spawn, spawned } = scriptedSpawn(() => {})
    const calls = stubGateway([404], spawned)
    const sup = new GatewaySupervisor(() => managedCli, managedDeps({ spawn }))
    try {
      const status = await sup.start()
      expect(status.state).toBe('running')
    } finally {
      vi.unstubAllGlobals()
    }
    expect(calls.filter((url) => url.endsWith('/ready')).length).toBeGreaterThanOrEqual(1)
    expect(calls.filter((url) => url.endsWith('/health')).length).toBeGreaterThanOrEqual(2)
  })
})

describe('summarizeDoctorReport', () => {
  it('names the first error that is not "the gateway is down", with its command', () => {
    const report = {
      ready: false,
      findings: [
        { id: 'gateway.unavailable', severity: 'error', title: 'Gateway is unavailable' },
        {
          id: 'config.local.unreadable',
          severity: 'error',
          title: 'Local configuration cannot be loaded',
          fixSteps: [{ label: 'Inspect' }, { label: 'Fix', command: 'agentos onboard' }],
        },
        { id: 'provider.key', severity: 'warn', title: 'Provider key missing' },
      ],
    }
    expect(summarizeDoctorReport(report)).toBe(
      'Local configuration cannot be loaded (fix: agentos onboard)',
    )
  })

  it('returns null when there is nothing beyond the gateway being down', () => {
    expect(
      summarizeDoctorReport({
        findings: [
          { id: 'gateway.unavailable', severity: 'error', title: 'Gateway is unavailable' },
        ],
      }),
    ).toBeNull()
    expect(summarizeDoctorReport(null)).toBeNull()
  })
})
