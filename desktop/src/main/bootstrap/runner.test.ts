// @vitest-environment node
import { EventEmitter } from 'node:events'
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { PassThrough } from 'node:stream'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { EngineDiscovery } from '@shared/bootstrap'
import { BootstrapRunner, type RunnerSpawner } from './runner'

const MANIFEST =
  '{"protocol_version":1,"stages":[' +
  '{"name":"prerequisites","title":"Check this Mac","category":"runtime","needs_user_input":false},' +
  '{"name":"uv","title":"Install uv","category":"runtime","needs_user_input":false},' +
  '{"name":"package","title":"Install the AgentOS engine","category":"runtime","needs_user_input":false}]}'

interface Script {
  stdout?: string[]
  stderr?: string[]
  code?: number
  hang?: boolean
}

function fakeSpawner(scripts: Record<string, Script>, calls: string[][] = []) {
  const spawn: RunnerSpawner = (command, args) => {
    calls.push([command, ...args])
    const key = args.includes('--manifest')
      ? 'manifest'
      : (args[args.indexOf('--stage') + 1] ?? '?')
    const script = scripts[key] ?? { code: 1, stderr: [`no script for ${key}`] }
    const child = new EventEmitter() as EventEmitter & {
      pid: number
      stdout: PassThrough
      stderr: PassThrough
      kill: () => boolean
    }
    child.pid = 777
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
    return child as unknown as ReturnType<RunnerSpawner>
  }
  return { spawn, calls }
}

const discovery: EngineDiscovery = {
  source: 'missing',
  cliPath: null,
  version: null,
  appVersion: '2026.9.12',
  relation: null,
  needsInstall: true,
  reason: 'missing',
}

let dir: string
beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), 'bootstrap-runner-'))
})
afterEach(() => rmSync(dir, { recursive: true, force: true }))

function make(scripts: Record<string, Script>) {
  const { spawn, calls } = fakeSpawner(scripts)
  let clock = 1_000
  const runner = new BootstrapRunner({
    scriptPath: '/app/Resources/install.sh',
    version: '2026.9.12',
    logDir: path.join(dir, 'logs'),
    cwd: dir,
    spawn,
    now: () => (clock += 250),
    kill: (child) => child.kill(),
  })
  return { runner, calls }
}

const ok = (stage: string, extra: string[] = []): Script => ({
  stdout: [...extra, `{"ok":true,"stage":"${stage}","skipped":false}`],
  stderr: [`install.sh: ${stage} progress`],
})

describe('BootstrapRunner', () => {
  it('reads the manifest, runs every stage in order, and writes a forensic log', async () => {
    const { runner, calls } = make({
      manifest: { stdout: [MANIFEST] },
      prerequisites: ok('prerequisites'),
      uv: ok('uv', ['install.sh: uv already installed: /u/.local/bin/uv']),
      package: ok('package'),
    })
    runner.offer(discovery)
    const phases: string[] = []
    runner.subscribe((s) => phases.push(s.phase))

    const state = await runner.run()

    expect(calls[0]).toEqual([
      'bash',
      '/app/Resources/install.sh',
      '--version',
      'v2026.9.12',
      '--manifest',
    ])
    expect(calls[1]).toEqual([
      'bash',
      '/app/Resources/install.sh',
      '--version',
      'v2026.9.12',
      '--stage',
      'prerequisites',
      '--json',
      '--non-interactive',
    ])
    expect(calls).toHaveLength(4)
    expect(state.phase).toBe('succeeded')
    expect(state.stages.map((s) => [s.name, s.state])).toEqual([
      ['prerequisites', 'succeeded'],
      ['uv', 'succeeded'],
      ['package', 'succeeded'],
    ])
    expect(state.stages.every((s) => s.durationMs !== null && s.durationMs > 0)).toBe(true)
    // Progress lines are logged; the JSON frames are not.
    expect(state.log.some((l) => l.line.includes('uv already installed'))).toBe(true)
    expect(state.log.some((l) => l.line.startsWith('{'))).toBe(false)
    expect(state.log.find((l) => l.line.includes('uv progress'))?.stream).toBe('stderr')
    expect([...new Set(phases)]).toEqual(['running', 'succeeded'])
    expect(state.logPath).not.toBeNull()
    expect(existsSync(state.logPath!)).toBe(true)
    expect(readFileSync(state.logPath!, 'utf8')).toContain('uv already installed')
  })

  it('stops at the first failed stage with its reason', async () => {
    const { runner, calls } = make({
      manifest: { stdout: [MANIFEST] },
      prerequisites: ok('prerequisites'),
      uv: {
        code: 1,
        stdout: [
          '{"ok":false,"stage":"uv","skipped":false,"reason":"stage \'uv\' failed (exit 1)"}',
        ],
        stderr: ['install.sh: could not download the uv installer'],
      },
      package: ok('package'),
    })
    const state = await runner.run()
    expect(state.phase).toBe('failed')
    expect(state.error).toContain('"Install uv" failed')
    expect(state.error).toContain('could not download')
    expect(state.stages.map((s) => s.state)).toEqual(['succeeded', 'failed', 'pending'])
    expect(calls).toHaveLength(3) // manifest + 2 stages, package never ran
  })

  it('treats a stage that vanished without a frame as failed', async () => {
    const { runner } = make({
      manifest: { stdout: [MANIFEST] },
      prerequisites: { code: 137, stderr: ['Killed'] },
    })
    const state = await runner.run()
    expect(state.phase).toBe('failed')
    expect(state.error).toContain('ended without a result (exit 137)')
  })

  it('fails cleanly when the manifest cannot be read', async () => {
    const { runner } = make({ manifest: { code: 1, stderr: ['bash: install.sh: No such file'] } })
    const state = await runner.run()
    expect(state.phase).toBe('failed')
    expect(state.error).toContain('stage list')
    expect(state.stages).toEqual([])
  })

  it('cancel kills the running stage and reports cancelled', async () => {
    const { runner } = make({
      manifest: { stdout: [MANIFEST] },
      prerequisites: ok('prerequisites'),
      uv: { hang: true },
    })
    const pending = runner.run()
    await new Promise((r) => setTimeout(r, 20))
    expect(runner.current().phase).toBe('running')
    runner.cancel()
    const state = await pending
    expect(state.phase).toBe('cancelled')
    expect(state.stages.map((s) => s.state)).toEqual(['succeeded', 'failed', 'pending'])
  })

  it('offer() picks update mode when an engine exists, install otherwise', () => {
    const { runner } = make({})
    expect(runner.offer(discovery).mode).toBe('install')
    expect(
      runner.offer({ ...discovery, cliPath: '/u/.local/bin/agentos', version: '2026.8.1' }).mode,
    ).toBe('update')
    expect(runner.dismiss().phase).toBe('ready')
  })
})
