import { describe, expect, it } from 'vitest'
import {
  calverToSemver,
  compareVersions,
  gatewaySupported,
  IDLE_ENGINE,
  idleAppState,
  isNewer,
  MIN_GATEWAY_VERSION,
  releaseUpdate,
  semverToCalver,
  type AppUpdateState,
  type EngineUpdateState,
} from './updates'

describe('releaseUpdate', () => {
  const engine = (patch: Partial<EngineUpdateState>): EngineUpdateState => ({
    ...IDLE_ENGINE,
    ...patch,
  })
  const app = (patch: Partial<AppUpdateState>): AppUpdateState => ({
    ...idleAppState('2026.9.22'),
    ...patch,
  })

  it('is nothing when both are current', () => {
    expect(
      releaseUpdate(
        engine({ availability: 'up-to-date' }),
        app({ phase: 'up-to-date' }),
        '2026.9.22',
      ),
    ).toEqual({ kind: 'none' })
  })

  it('names the release when either side is outdated, and which sides', () => {
    expect(
      releaseUpdate(
        engine({ availability: 'outdated', latest: '2026.9.23' }),
        app({ phase: 'available', latest: '2026.9.23' }),
        null,
      ),
    ).toEqual({ kind: 'available', version: '2026.9.23', engine: true, app: true })
    expect(
      releaseUpdate(
        engine({ availability: 'outdated', latest: '2026.9.23' }),
        app({ phase: 'up-to-date' }),
        null,
      ),
    ).toMatchObject({ kind: 'available', engine: true, app: false })
    // Two different "latest"s (the app build lagging the wheel): the newer wins the label.
    expect(
      releaseUpdate(
        engine({ availability: 'outdated', latest: '2026.9.23.post1' }),
        app({ phase: 'available', latest: '2026.9.23' }),
        null,
      ),
    ).toMatchObject({ version: '2026.9.23.post1' })
  })

  it('reports the running step, engine first, then the app download', () => {
    expect(
      releaseUpdate(
        engine({ phase: 'installing', latest: '2026.9.23' }),
        app({ phase: 'available', latest: '2026.9.23' }),
        null,
      ),
    ).toEqual({ kind: 'working', version: '2026.9.23', step: 'engine', percent: null })
    expect(
      releaseUpdate(
        engine({ phase: 'done' }),
        app({ phase: 'downloading', latest: '2026.9.23', percent: 40 }),
        null,
      ),
    ).toEqual({ kind: 'working', version: '2026.9.23', step: 'app', percent: 40 })
  })

  it('ends on the relaunch, carrying the block reason', () => {
    expect(
      releaseUpdate(
        engine({ phase: 'installing' }),
        app({ phase: 'downloaded', latest: '2026.9.23', blocked: 'engine-updating' }),
        null,
      ),
    ).toEqual({ kind: 'restart', version: '2026.9.23', blocked: 'engine-updating' })
  })

  it('keeps a failed download or restart visible, with its error', () => {
    expect(
      releaseUpdate(
        engine({}),
        app({ phase: 'error', latest: '2026.9.23', error: 'net::ERR_NETWORK_CHANGED' }),
        null,
      ),
    ).toEqual({ kind: 'failed', version: '2026.9.23', error: 'net::ERR_NETWORK_CHANGED' })
    // A check that failed before any newer build was known is About's business only.
    expect(releaseUpdate(engine({}), app({ phase: 'error', error: '403' }), null)).toEqual({
      kind: 'none',
    })
  })

  it('notices an engine upgraded from a terminal that the gateway is not running yet', () => {
    expect(
      releaseUpdate(
        engine({ current: '2026.9.23', availability: 'up-to-date' }),
        app({ phase: 'up-to-date' }),
        '2026.9.22+abc',
      ),
    ).toEqual({ kind: 'gateway-restart', version: '2026.9.23', running: '2026.9.22' })
    expect(
      releaseUpdate(engine({ current: '2026.9.23' }), app({ phase: 'up-to-date' }), '2026.9.23'),
    ).toEqual({ kind: 'none' })
  })

  describe('external gateway', () => {
    const external = { external: true }

    it('never asks to restart a gateway this app does not run', () => {
      // A newer engine on this Mac's disk says nothing about a gateway on
      // another host; the old answer was a "Restart gateway" that would
      // have restarted nothing.
      expect(
        releaseUpdate(
          engine({ current: '2026.11.3', latest: '2026.11.3', availability: 'up-to-date' }),
          app({ phase: 'up-to-date', current: '2026.11.3', latest: '2026.11.3' }),
          '2026.11.3',
          external,
        ),
      ).toEqual({ kind: 'none' })
    })

    it('reports a remote gateway behind the latest release', () => {
      expect(
        releaseUpdate(
          engine({ current: '2026.11.3', latest: '2026.11.3', availability: 'up-to-date' }),
          app({ phase: 'up-to-date', current: '2026.11.3', latest: '2026.11.3' }),
          '2026.11.2+abc',
          external,
        ),
      ).toEqual({ kind: 'gateway-outdated', version: '2026.11.3', running: '2026.11.2' })
    })

    it('takes the latest version from the app check when the local engine was not checked', () => {
      expect(
        releaseUpdate(
          engine({}),
          app({ phase: 'up-to-date', current: '2026.11.3', latest: '2026.11.3' }),
          '2026.11.2',
          external,
        ),
      ).toEqual({ kind: 'gateway-outdated', version: '2026.11.3', running: '2026.11.2' })
      // No latest known from either check: nothing to compare against.
      expect(releaseUpdate(engine({}), app({}), '2026.11.2', external)).toEqual({ kind: 'none' })
    })

    it('ignores an outdated local engine: it is not the one running', () => {
      expect(
        releaseUpdate(
          engine({ current: '2026.11.2', latest: '2026.11.3', availability: 'outdated' }),
          app({ phase: 'up-to-date', current: '2026.11.3', latest: '2026.11.3' }),
          '2026.11.3',
          external,
        ),
      ).toEqual({ kind: 'none' })
    })

    it('still offers the app update first, then the gateway notice', () => {
      expect(
        releaseUpdate(
          engine({ current: '2026.11.2', latest: '2026.11.3', availability: 'outdated' }),
          app({ phase: 'available', current: '2026.11.2', latest: '2026.11.3' }),
          '2026.11.2',
          external,
        ),
      ).toEqual({ kind: 'available', version: '2026.11.3', engine: false, app: true })
    })
  })
})

describe('calverToSemver / semverToCalver', () => {
  it('folds month and day into the minor and keeps .postN as the patch', () => {
    expect(calverToSemver('2026.9.22')).toBe('2026.922.0')
    expect(calverToSemver('2026.9.22.post1')).toBe('2026.922.1')
    expect(calverToSemver('v2026.10.1')).toBe('2026.1001.0')
    expect(calverToSemver('2026.9.2-2.post1')).toBeNull()
    expect(calverToSemver('1.2.3')).toBeNull()
  })

  it('keeps electron-updater ordering the way CalVer orders', () => {
    const semver = (v: string) => calverToSemver(v)!
    const gt = (a: string, b: string) => compareVersions(semver(a), semver(b)) > 0
    expect(gt('2026.9.22.post1', '2026.9.22')).toBe(true)
    expect(gt('2026.9.23', '2026.9.22.post1')).toBe(true)
    expect(gt('2026.10.1', '2026.9.30')).toBe(true)
    // A pre-scheme release is smaller than every twin, so it upgrades.
    expect(compareVersions('2026.922.0', '2026.9.20')).toBeGreaterThan(0)
  })

  it('round-trips, and leaves a plain old release alone', () => {
    for (const v of ['2026.9.22', '2026.9.22.post1', '2026.10.1', '2026.12.31.post12']) {
      expect(semverToCalver(calverToSemver(v)!)).toBe(v)
    }
    expect(semverToCalver('2026.9.20')).toBe('2026.9.20')
    expect(semverToCalver('2026.1399.0')).toBe('2026.1399.0')
  })
})

describe('compareVersions', () => {
  it('orders CalVer numerically, not lexically', () => {
    expect(isNewer('2026.9.11', '2026.9.9')).toBe(true)
    expect(isNewer('2026.10.1', '2026.9.30')).toBe(true)
    expect(isNewer('2027.1.1', '2026.12.31')).toBe(true)
    expect(isNewer('2026.9.9', '2026.9.9')).toBe(false)
  })

  it('treats .postN as after the release but before the next one', () => {
    expect(isNewer('2026.9.9.post1', '2026.9.9')).toBe(true)
    expect(isNewer('2026.9.10', '2026.9.9.post1')).toBe(true)
    expect(isNewer('2026.9.9.post2', '2026.9.9.post1')).toBe(true)
  })

  it('sorts pre-releases and local builds below the plain release', () => {
    expect(compareVersions('2026.9.9rc1', '2026.9.9')).toBeLessThan(0)
    expect(compareVersions('0.0.0+unknown', '0.0.0')).toBeLessThan(0)
    expect(compareVersions('v2026.9.9', '2026.9.9')).toBe(0)
  })
})

describe('gatewaySupported', () => {
  it('accepts the minimum and anything newer, including build suffixes', () => {
    expect(gatewaySupported(MIN_GATEWAY_VERSION)).toBe(true)
    expect(gatewaySupported(`${MIN_GATEWAY_VERSION}+abc123`)).toBe(true)
    expect(gatewaySupported('2999.1.1')).toBe(true)
  })

  it('rejects an older gateway and tolerates an unknown one', () => {
    expect(gatewaySupported('2026.8.23')).toBe(false)
    expect(gatewaySupported(null)).toBe(true)
    expect(gatewaySupported(undefined)).toBe(true)
  })
})
