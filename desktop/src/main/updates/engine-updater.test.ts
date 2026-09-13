// @vitest-environment node
import { EventEmitter } from 'node:events'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { PassThrough } from 'node:stream'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { GatewayStatus } from '@shared/gateway'
import type { GatewaySettings } from '@shared/settings'
import {
  defaultMarkerPath,
  EngineUpdater,
  hardenedEnv,
  lastJson,
  MARKER_MAX_AGE_MS,
  type Spawner,
} from './engine-updater'

const managed: GatewaySettings = {
  mode: 'managed',
  host: '127.0.0.1',
  port: 18791,
  token: null,
  cliPath: '/opt/agentos',
}

/** A scripted child: emit lines on stdout/stderr, then exit with a code. */
interface Script {
  stdout?: string[]
  stderr?: string[]
  code?: number | null
  /** Leave the process running (for timeout tests). */
  hang?: boolean
}

function fakeSpawner(scripts: Record<string, Script>, calls: string[][] = []) {
  const spawn: Spawner = (command, args) => {
    calls.push([command, ...args])
    const key = args.join(' ')
    const script = scripts[key] ?? { code: 1, stderr: [`no script for ${key}`] }
    const child = new EventEmitter() as EventEmitter & {
      pid: number
      stdout: PassThrough
      stderr: PassThrough
      kill: () => boolean
    }
    child.pid = 4242
    child.stdout = new PassThrough()
    child.stderr = new PassThrough()
    child.kill = () => {
      child.emit('exit', null)
      return true
    }
    queueMicrotask(() => {
      for (const line of script.stdout ?? []) child.stdout.write(`${line}\n`)
      for (const line of script.stderr ?? []) child.stderr.write(`${line}\n`)
      if (!script.hang) setTimeout(() => child.emit('exit', script.code ?? 0), 0)
    })
    return child as unknown as ReturnType<Spawner>
  }
  return { spawn, calls }
}

function gatewayStub(status: Partial<GatewayStatus> = {}) {
  const current: GatewayStatus = {
    state: 'running',
    pid: 99,
    url: 'http://127.0.0.1:18791',
    error: null,
    ...status,
  }
  const restart = vi.fn(async (): Promise<GatewayStatus> => ({ ...current, pid: 100 }))
  return { current: () => current, restart }
}

let dir: string
let markerPath: string

beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), 'engine-updater-'))
  markerPath = path.join(dir, 'engine-update.json')
})
afterEach(() => rmSync(dir, { recursive: true, force: true }))

function make(
  scripts: Record<string, Script>,
  opts: { settings?: GatewaySettings; gateway?: ReturnType<typeof gatewayStub> } = {},
) {
  const { spawn, calls } = fakeSpawner(scripts)
  const gateway = opts.gateway ?? gatewayStub()
  const updater = new EngineUpdater({
    getSettings: () => opts.settings ?? managed,
    gateway,
    markerPath,
    spawn,
    locate: (override) => override,
    isPidAlive: () => false,
    now: () => 1_000_000,
    kill: (child) => child.kill(),
  })
  return { updater, calls, gateway }
}

describe('EngineUpdater.check', () => {
  it('reads current/latest/status from `agentos upgrade --check --json`', async () => {
    const { updater, calls } = make({
      'upgrade --check --json': {
        stderr: ['2026-09-12 [debug    ] env.loaded key=X'],
        stdout: ['{"current": "2026.8.23", "latest": "2026.9.11", "status": "outdated"}'],
      },
    })
    const seen: string[] = []
    updater.subscribe((s) => seen.push(s.phase))
    const state = await updater.check()
    expect(calls[0]).toEqual(['/opt/agentos', 'upgrade', '--check', '--json'])
    expect(state).toMatchObject({
      phase: 'idle',
      current: '2026.8.23',
      latest: '2026.9.11',
      availability: 'outdated',
      checkedAt: 1_000_000,
    })
    expect(seen).toEqual(['checking', 'idle'])
  })

  it('reports an error when the CLI cannot be found', async () => {
    const { updater } = make({}, { settings: { ...managed, cliPath: null } })
    const state = await updater.check()
    expect(state.phase).toBe('error')
    expect(state.error).toContain('agentos CLI not found')
  })

  it('reports a failing check with the useful part of stderr', async () => {
    const { updater } = make({
      'upgrade --check --json': {
        code: 1,
        stderr: ['[info     ] env.injected count=3', 'Traceback…', 'ValidationError: boom'],
      },
    })
    const state = await updater.check()
    expect(state.phase).toBe('error')
    expect(state.error).toContain('exit 1')
    expect(state.error).toContain('ValidationError: boom')
    expect(state.error).not.toContain('env.injected')
  })
})

describe('EngineUpdater.apply', () => {
  const okInstall: Script = {
    stdout: [
      'Snapshot: 3 file(s) → /home/.agentos/state/snapshots/pre-upgrade-1',
      'Upgrading use-agent-os via uv-tool from pypi…',
      'Upgraded: 2026.8.23 → 2026.9.11',
      '{"old": "2026.8.23", "new": "2026.9.11", "source": "pypi", "restarted": false, "snapshot": {"path": "/home/.agentos/state/snapshots/pre-upgrade-1", "files": 3}}',
    ],
  }

  it('runs the installer with --no-restart, streams the log, restarts the managed gateway', async () => {
    const gateway = gatewayStub()
    const { updater, calls } = make({ 'upgrade --json --no-restart': okInstall }, { gateway })
    const phases: string[] = []
    updater.subscribe((s) => phases.push(s.phase))

    const state = await updater.apply()

    expect(calls[0]).toEqual(['/opt/agentos', 'upgrade', '--json', '--no-restart'])
    expect(gateway.restart).toHaveBeenCalledTimes(1)
    expect(state.phase).toBe('done')
    expect(state.result).toEqual({
      old: '2026.8.23',
      new: '2026.9.11',
      gatewayRestarted: true,
      snapshot: '/home/.agentos/state/snapshots/pre-upgrade-1',
      source: 'pypi',
    })
    expect(state.current).toBe('2026.9.11')
    expect(state.availability).toBe('up-to-date')
    expect(state.log).toContain('Upgrading use-agent-os via uv-tool from pypi…')
    expect(state.log).toContain('Installed 2026.8.23 → 2026.9.11.')
    expect(state.log).toContain('Gateway restarted.')
    expect([...new Set(phases)]).toEqual(['installing', 'restarting', 'done'])
    expect(existsSync(markerPath)).toBe(false)
  })

  it('writes a marker while the installer runs and removes it after', async () => {
    let markerDuringRun: string | null = null
    const gateway = gatewayStub()
    gateway.restart.mockImplementation(async () => {
      markerDuringRun = readFileSync(markerPath, 'utf8')
      return { state: 'running', pid: 100, url: 'x', error: null }
    })
    const { updater } = make({ 'upgrade --json --no-restart': okInstall }, { gateway })
    await updater.apply()
    expect(markerDuringRun).not.toBeNull()
    expect(JSON.parse(markerDuringRun!)).toEqual({ pid: 4242, startedAt: 1_000_000 })
    expect(existsSync(markerPath)).toBe(false)
  })

  it('does not restart a gateway it did not spawn, and says so', async () => {
    const gateway = gatewayStub({ pid: null })
    const { updater } = make({ 'upgrade --json --no-restart': okInstall }, { gateway })
    const state = await updater.apply()
    expect(gateway.restart).not.toHaveBeenCalled()
    expect(state.phase).toBe('done')
    expect(state.result?.gatewayRestarted).toBe(false)
    expect(state.log.at(-1)).toContain('not started by this app')
  })

  it('refuses in external mode', async () => {
    const gateway = gatewayStub()
    const { updater, calls } = make(
      { 'upgrade --json --no-restart': okInstall },
      { settings: { ...managed, mode: 'external' }, gateway },
    )
    const state = await updater.apply()
    expect(calls).toEqual([])
    expect(state.phase).toBe('error')
    expect(state.error).toContain('external mode')
  })

  it('surfaces the manual command for a non-delegated install (exit 3)', async () => {
    const { updater, gateway } = make({
      'upgrade --json --no-restart': {
        code: 3,
        stdout: [
          '{"method": "pip", "delegated": false, "manualCommand": "python -m pip install --upgrade \\"use-agent-os[recommended]\\""}',
        ],
      },
    })
    const state = await updater.apply()
    expect(state.phase).toBe('error')
    expect(state.manualCommand).toBe('python -m pip install --upgrade "use-agent-os[recommended]"')
    expect(gateway.restart).not.toHaveBeenCalled()
    expect(existsSync(markerPath)).toBe(false)
  })

  it('reports an installer failure with stderr and leaves the gateway alone', async () => {
    const { updater, gateway } = make({
      'upgrade --json --no-restart': { code: 1, stderr: ['error: failed to resolve wheel'] },
    })
    const state = await updater.apply()
    expect(state.phase).toBe('error')
    expect(state.error).toContain('exit 1')
    expect(state.error).toContain('failed to resolve wheel')
    expect(gateway.restart).not.toHaveBeenCalled()
  })

  it('keeps the upgraded result but errors when the gateway does not come back', async () => {
    const gateway = gatewayStub()
    gateway.restart.mockResolvedValue({ state: 'error', pid: null, url: null, error: 'boom' })
    const { updater } = make({ 'upgrade --json --no-restart': okInstall }, { gateway })
    const state = await updater.apply()
    expect(state.phase).toBe('error')
    expect(state.error).toContain('did not come back')
    expect(state.result?.new).toBe('2026.9.11')
  })

  it('ignores a second apply while one is running', async () => {
    const { updater, calls } = make({ 'upgrade --json --no-restart': okInstall })
    const first = updater.apply()
    const second = await updater.apply()
    expect(second.phase).toBe('installing')
    await first
    expect(calls).toHaveLength(1)
  })
})

describe('EngineUpdater.recover', () => {
  it('does nothing without a marker', () => {
    const { updater } = make({})
    expect(updater.recover().interrupted).toBe(false)
  })

  it('flags a stale marker as an interrupted update and removes it', () => {
    writeFileSync(
      markerPath,
      JSON.stringify({ pid: 1, startedAt: 1_000_000 - MARKER_MAX_AGE_MS - 1 }),
    )
    const { updater } = make({})
    const state = updater.recover()
    expect(state.interrupted).toBe(true)
    expect(state.error).toContain('did not finish')
    expect(existsSync(markerPath)).toBe(false)
  })

  it('keeps a fresh marker whose installer is still alive', () => {
    writeFileSync(markerPath, JSON.stringify({ pid: 1, startedAt: 1_000_000 - 1000 }))
    const gateway = gatewayStub()
    const updater = new EngineUpdater({
      getSettings: () => managed,
      gateway,
      markerPath,
      spawn: fakeSpawner({}).spawn,
      locate: (o) => o,
      isPidAlive: () => true,
      now: () => 1_000_000,
    })
    const state = updater.recover()
    expect(state.interrupted).toBe(true)
    expect(state.error).toContain('still running')
    expect(existsSync(markerPath)).toBe(true)
  })
})

describe('helpers', () => {
  it('lastJson picks the last JSON object line, ignoring prose', () => {
    expect(lastJson('Upgrading…\n{"a": 1}\nUpgraded\n{"b": 2}\n')).toEqual({ b: 2 })
    expect(lastJson('nothing here')).toBeNull()
    expect(lastJson('{not json}')).toBeNull()
  })

  it('hardenedEnv appends the login-shell dirs and silences the update notice', () => {
    const env = hardenedEnv({ PATH: '/usr/bin' })
    expect(env.PATH!.split(path.delimiter)).toContain('/opt/homebrew/bin')
    expect(env.PATH!.split(path.delimiter).filter((p) => p === '/usr/bin')).toHaveLength(1)
    expect(env.AGENTOS_NO_UPDATE_NOTICE).toBe('1')
  })

  it('defaultMarkerPath follows AGENTOS_STATE_DIR', () => {
    expect(defaultMarkerPath({})).toMatch(/\.agentos\/state\/desktop\/engine-update\.json$/)
    expect(defaultMarkerPath({ AGENTOS_STATE_DIR: '/tmp/x' })).toBe(
      '/tmp/x/state/desktop/engine-update.json',
    )
  })
})
