// @vitest-environment node
import { describe, expect, it, vi } from 'vitest'
import type { BootstrapState } from '@shared/bootstrap'
import type { GatewaySettings, SettingsPatch } from '@shared/settings'
import { BootstrapController } from './controller'
import type { ProbeResult } from './discovery'
import type { BootstrapRunner } from './runner'

const managed: GatewaySettings = {
  mode: 'managed',
  host: '127.0.0.1',
  port: 18791,
  token: null,
  cliPath: null,
}

function fakeRunner(outcome: BootstrapState['phase'] = 'succeeded') {
  const runner = {
    markReady: vi.fn((d) => ({ phase: 'ready', discovery: d }) as BootstrapState),
    offer: vi.fn((d) => ({ phase: 'choice', discovery: d }) as BootstrapState),
    dismiss: vi.fn(() => ({ phase: 'ready' }) as BootstrapState),
    run: vi.fn(async () => ({ phase: outcome }) as BootstrapState),
  }
  return runner as unknown as BootstrapRunner & typeof runner
}

function make(opts: {
  settings?: GatewaySettings
  probe?: (cli: string) => Promise<ProbeResult>
  locate?: (override: string | null) => string | null
  outcome?: BootstrapState['phase']
  /** A gateway already answering on the endpoint (terminal, earlier launch). */
  runningGateway?: boolean
}) {
  let settings = opts.settings ?? managed
  const patches: SettingsPatch[] = []
  const gateway = {
    start: vi.fn(async () => ({ state: 'running', pid: 1, url: 'x', error: null }) as const),
    stop: vi.fn(async () => ({ state: 'stopped', pid: null, url: null, error: null }) as const),
    current: () => ({ state: 'running', pid: 1, url: 'x', error: null }) as const,
    adopt: vi.fn(async () =>
      opts.runningGateway
        ? ({ state: 'running', pid: null, url: 'x', error: null } as const)
        : null,
    ),
  }
  const runner = fakeRunner(opts.outcome)
  const controller = new BootstrapController({
    runner,
    gateway,
    getSettings: () => settings,
    updateSettings: (patch) => {
      patches.push(patch)
      settings = { ...settings, ...(patch.gateway ?? {}) }
    },
    appVersion: '2026.9.12',
    locate: opts.locate ?? (() => null),
    probe: opts.probe ?? (async () => ({ ok: false, version: null, detail: 'none' })),
  })
  return { controller, runner, gateway, patches }
}

describe('BootstrapController.launch', () => {
  it('starts the gateway straight away when the engine matches', async () => {
    const { controller, runner, gateway } = make({
      locate: () => '/u/.local/bin/agentos',
      probe: async () => ({ ok: true, version: '2026.9.12', detail: '' }),
    })
    const state = await controller.launch()
    expect(state.phase).toBe('ready')
    expect(gateway.start).toHaveBeenCalledTimes(1)
    expect(runner.offer).not.toHaveBeenCalled()
  })

  it('offers the install and does NOT start a gateway when the engine is missing', async () => {
    const { controller, runner, gateway } = make({})
    const state = await controller.launch()
    expect(state.phase).toBe('choice')
    expect(runner.offer).toHaveBeenCalledTimes(1)
    expect(gateway.start).not.toHaveBeenCalled()
  })

  it('adopts a gateway that is already running instead of blocking on an install', async () => {
    const { controller, runner, gateway } = make({
      runningGateway: true,
      locate: () => '/u/.local/bin/agentos',
      probe: async () => ({ ok: true, version: '2026.8.23', detail: '' }),
    })
    const state = await controller.launch()
    expect(state.phase).toBe('ready')
    expect(gateway.adopt).toHaveBeenCalledTimes(1)
    expect(gateway.start).not.toHaveBeenCalled()
    expect(runner.offer).not.toHaveBeenCalled()
  })

  it('skips discovery-driven installs in external mode', async () => {
    const { controller, runner, gateway } = make({ settings: { ...managed, mode: 'external' } })
    const state = await controller.launch()
    expect(state.phase).toBe('ready')
    expect(gateway.start).toHaveBeenCalledTimes(1)
    expect(runner.offer).not.toHaveBeenCalled()
  })
})

describe('BootstrapController.install', () => {
  it('runs the stages, drops a dead cliPath override, then starts the gateway', async () => {
    const { controller, gateway, patches } = make({
      settings: { ...managed, cliPath: '/gone/agentos' },
      locate: () => null,
    })
    const state = await controller.install()
    expect(state.phase).toBe('succeeded')
    expect(patches).toEqual([{ gateway: { cliPath: null } }])
    expect(gateway.start).toHaveBeenCalledTimes(1)
  })

  it('leaves the gateway alone when the install fails', async () => {
    const { controller, gateway } = make({ outcome: 'failed' })
    const state = await controller.install()
    expect(state.phase).toBe('failed')
    expect(gateway.start).not.toHaveBeenCalled()
  })
})

describe('BootstrapController.connectExisting', () => {
  it('switches to external mode and connects', async () => {
    const { controller, gateway, patches, runner } = make({})
    const state = await controller.connectExisting()
    expect(patches).toEqual([{ gateway: { mode: 'external' } }])
    expect(gateway.start).toHaveBeenCalledTimes(1)
    expect(runner.dismiss).toHaveBeenCalledTimes(1)
    expect(state.phase).toBe('ready')
  })
})

describe('BootstrapController.reinstall', () => {
  it('forces an install even when the engine matches', async () => {
    const { controller, runner } = make({
      locate: () => '/u/.local/bin/agentos',
      probe: async () => ({ ok: true, version: '2026.9.12', detail: '' }),
    })
    const state = await controller.reinstall()
    expect(runner.offer).toHaveBeenCalledWith(expect.objectContaining({ needsInstall: true }))
    expect(runner.run).toHaveBeenCalledTimes(1)
    expect(state.phase).toBe('succeeded')
  })
})
