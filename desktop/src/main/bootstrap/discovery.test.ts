// @vitest-environment node
import { describe, expect, it } from 'vitest'
import type { GatewaySettings } from '@shared/settings'
import { discoverEngine, type ProbeResult } from './discovery'

const managed: GatewaySettings = {
  mode: 'managed',
  host: '127.0.0.1',
  port: 18791,
  token: null,
  cliPath: null,
}

function probeReturning(map: Record<string, ProbeResult>) {
  return async (cli: string): Promise<ProbeResult> =>
    map[cli] ?? { ok: false, version: null, detail: 'not runnable' }
}

describe('discoverEngine', () => {
  it('needs an install when nothing is found', async () => {
    const d = await discoverEngine({
      settings: managed,
      appVersion: '2026.9.12',
      locate: () => null,
      probe: probeReturning({}),
    })
    expect(d).toMatchObject({ source: 'missing', needsInstall: true, cliPath: null })
  })

  it('uses a matching engine as-is', async () => {
    const d = await discoverEngine({
      settings: managed,
      appVersion: '2026.9.12',
      locate: () => '/u/.local/bin/agentos',
      probe: probeReturning({
        '/u/.local/bin/agentos': { ok: true, version: '2026.9.12', detail: '' },
      }),
    })
    expect(d).toMatchObject({ source: 'found', relation: 'same', needsInstall: false })
  })

  it('updates over an older engine, and over one too old to say its version', async () => {
    const older = await discoverEngine({
      settings: managed,
      appVersion: '2026.9.12',
      locate: () => '/u/.local/bin/agentos',
      probe: probeReturning({
        '/u/.local/bin/agentos': { ok: true, version: '2026.8.23', detail: '' },
      }),
    })
    expect(older).toMatchObject({ relation: 'older', needsInstall: true })
    expect(older.reason).toContain('2026.8.23')

    const ancient = await discoverEngine({
      settings: managed,
      appVersion: '2026.9.12',
      locate: () => '/u/.local/bin/agentos',
      probe: probeReturning({
        '/u/.local/bin/agentos': { ok: true, version: null, detail: 'engine predates --version' },
      }),
    })
    expect(ancient).toMatchObject({ relation: null, needsInstall: true, version: null })
  })

  it('keeps a newer engine', async () => {
    const d = await discoverEngine({
      settings: managed,
      appVersion: '2026.9.12',
      locate: () => '/u/.local/bin/agentos',
      probe: probeReturning({
        '/u/.local/bin/agentos': { ok: true, version: '2026.10.1+abc', detail: '' },
      }),
    })
    expect(d).toMatchObject({ relation: 'newer', needsInstall: false })
  })

  it('reinstalls over a CLI that does not start', async () => {
    const d = await discoverEngine({
      settings: managed,
      appVersion: '2026.9.12',
      locate: () => '/u/.local/bin/agentos',
      probe: probeReturning({
        '/u/.local/bin/agentos': { ok: false, version: null, detail: 'exit 1: ImportError' },
      }),
    })
    expect(d.needsInstall).toBe(true)
    expect(d.reason).toContain('ImportError')
  })

  it("never installs over the user's explicit cliPath", async () => {
    const d = await discoverEngine({
      settings: { ...managed, cliPath: '/checkout/.venv/bin/agentos' },
      appVersion: '2026.9.12',
      locate: (override) => override,
      probe: probeReturning({
        '/checkout/.venv/bin/agentos': { ok: true, version: '2026.1.1', detail: '' },
      }),
    })
    expect(d).toMatchObject({ source: 'override', relation: 'older', needsInstall: false })
  })

  it('falls through to discovery when the override is not executable', async () => {
    const d = await discoverEngine({
      settings: { ...managed, cliPath: '/gone/agentos' },
      appVersion: '2026.9.12',
      locate: (override) => (override ? null : '/u/.local/bin/agentos'),
      probe: probeReturning({
        '/u/.local/bin/agentos': { ok: true, version: '2026.9.12', detail: '' },
      }),
    })
    expect(d).toMatchObject({ source: 'found', needsInstall: false })
  })
})
