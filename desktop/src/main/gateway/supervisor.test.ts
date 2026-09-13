// @vitest-environment node
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'
import { describe, expect, it, vi } from 'vitest'
import type { GatewaySettings } from '@shared/settings'
import { GatewaySupervisor } from './supervisor'

const external: GatewaySettings = {
  mode: 'external',
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

describe('GatewaySupervisor (spawn arguments)', () => {
  it('binds host and port on the command line and passes the token through AuthConfig env', async () => {
    const managed: GatewaySettings = {
      mode: 'managed',
      host: '127.0.0.1',
      port: 18999,
      token: 'secret',
      cliPath: '/opt/agentos',
    }
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
    let probes = 0
    const sup = new GatewaySupervisor(() => managed, {
      // Not running before the spawn; healthy right after it.
      probe: async () => probes++ > 0,
      locate: (override) => override,
      spawn: spawn as unknown as typeof import('node:child_process').spawn,
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
    // The old double-underscore names never reached the gateway's settings.
    expect(spawnCalls[0]?.env.AGENTOS_GATEWAY__PORT).toBeUndefined()
    expect(spawnCalls[0]?.env.AGENTOS_AUTH__TOKEN).toBeUndefined()
  })
})
