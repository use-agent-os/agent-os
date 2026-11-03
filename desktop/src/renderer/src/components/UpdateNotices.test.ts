import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReleaseUpdate } from '@shared/updates'

const toastFn = Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() })
vi.mock('sonner', () => ({ toast: toastFn }))

const { announceRelease } = await import('./UpdateNotices')

const none: ReleaseUpdate = { kind: 'none' }
const available: ReleaseUpdate = {
  kind: 'available',
  version: '2026.9.23',
  engine: true,
  app: true,
}
const engineOnly: ReleaseUpdate = {
  kind: 'available',
  version: '2026.9.23',
  engine: true,
  app: false,
}
const working: ReleaseUpdate = {
  kind: 'working',
  version: '2026.9.23',
  step: 'engine',
  percent: null,
}
const restart: ReleaseUpdate = { kind: 'restart', version: '2026.9.23', blocked: null }

const act = {
  update: vi.fn(),
  restart: vi.fn(),
  restartGateway: vi.fn(),
  retry: vi.fn(),
  details: vi.fn(),
}
type Opts = { action: { onClick(): void }; description?: string; id: string }

beforeEach(() => {
  toastFn.mockClear()
  toastFn.success.mockClear()
  toastFn.error.mockClear()
  toastFn.dismiss.mockClear()
  act.update.mockClear()
  act.restart.mockClear()
  act.restartGateway.mockClear()
  act.details.mockClear()
})

describe('announceRelease', () => {
  it('offers one Update for the release, once, and says when both sides move', () => {
    announceRelease(none, available, act)
    announceRelease(available, { ...available }, act)
    expect(toastFn).toHaveBeenCalledTimes(1)
    const [text, opts] = toastFn.mock.calls[0] as [string, Opts]
    expect(text).toContain('2026.9.23')
    expect(opts.id).toBe('agentos-release')
    expect(opts.description).toMatch(/engine updates in place/)
    opts.action.onClick()
    expect(act.update).toHaveBeenCalledTimes(1)

    announceRelease(none, engineOnly, act)
    expect((toastFn.mock.calls[1] as [string, Opts])[1].description).toBeNull()
  })

  it('goes quiet while working, then offers the restart with its block reason', () => {
    announceRelease(available, working, act)
    expect(toastFn.dismiss).toHaveBeenCalledWith('agentos-release')

    announceRelease(working, restart, act)
    expect(toastFn.success).toHaveBeenCalledTimes(1)
    // The card keeps its id across steps, so the last description is cleared explicitly.
    expect((toastFn.success.mock.calls[0] as [string, Opts])[1].description).toBeNull()
    ;(toastFn.success.mock.calls[0] as [string, Opts])[1].action.onClick()
    expect(act.restart).toHaveBeenCalledTimes(1)

    announceRelease(restart, { ...restart, blocked: 'engine-updating' }, act)
    expect(toastFn.success).toHaveBeenCalledTimes(2)
    expect((toastFn.success.mock.calls[1] as [string])[0]).toMatch(/engine is being updated/)
  })

  it('offers Try again after a failed download, and a relaunch hint when Squirrel refused', () => {
    const failed: ReleaseUpdate = {
      kind: 'failed',
      version: '2026.9.23',
      error: 'net::ERR_NETWORK_CHANGED',
    }
    announceRelease(available, failed, act)
    expect(toastFn.error).toHaveBeenCalledTimes(1)
    const [text, opts] = toastFn.error.mock.calls[0] as [string, Opts]
    expect(text).toBe('Could not update to AgentOS 2026.9.23.')
    expect(opts.description).toBe('net::ERR_NETWORK_CHANGED')
    opts.action.onClick()
    expect(act.retry).toHaveBeenCalledTimes(1)

    announceRelease(
      failed,
      { ...failed, error: 'The command is disabled and cannot be executed' },
      act,
    )
    expect((toastFn.error.mock.calls[1] as [string])[0]).toMatch(/Quit and reopen AgentOS/)
  })

  it('says an external gateway is behind and only opens About for it', () => {
    const behind: ReleaseUpdate = {
      kind: 'gateway-outdated',
      version: '2026.11.3',
      running: '2026.11.2',
    }
    announceRelease(none, behind, act)
    announceRelease(behind, { ...behind }, act)
    expect(toastFn).toHaveBeenCalledTimes(1)
    const [text, opts] = toastFn.mock.calls[0] as [string, Opts]
    expect(text).toContain('2026.11.3')
    expect(opts.description).toMatch(/runs 2026\.11\.2 on its own host/)
    opts.action.onClick()
    expect(act.details).toHaveBeenCalledTimes(1)
    expect(act.restartGateway).not.toHaveBeenCalled()
  })

  it('says an engine-only update finished, and offers the gateway restart after a terminal upgrade', () => {
    announceRelease(working, none, act)
    expect(toastFn.success).toHaveBeenCalledTimes(1)
    expect((toastFn.success.mock.calls[0] as [string])[0]).toMatch(/Engine updated to 2026.9.23/)

    const gw: ReleaseUpdate = {
      kind: 'gateway-restart',
      version: '2026.9.23',
      running: '2026.9.22',
    }
    announceRelease(none, gw, act)
    const [text, opts] = toastFn.mock.calls[0] as [string, Opts]
    expect(text).toBe('Engine 2026.9.23 is installed.')
    expect(opts.description).toBe('The gateway is still running 2026.9.22.')
    opts.action.onClick()
    expect(act.restartGateway).toHaveBeenCalledTimes(1)

    announceRelease(gw, none, act)
    expect(toastFn.dismiss).toHaveBeenCalledWith('agentos-release')
  })
})
