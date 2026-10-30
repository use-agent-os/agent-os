import { describe, expect, it, vi } from 'vitest'

import type * as FlowRunModule from './flowRun'
import { createLazyFlowRunMounter } from './flowRunLazy'

function fakeModule() {
  const real = {
    mountFlowRuns: vi.fn(),
    runEvent: vi.fn(),
    reconnected: vi.fn(),
    destroyAll: vi.fn(),
    pruneDetached: vi.fn(),
    refresh: vi.fn(),
  }
  const mod = { createFlowRunMounter: vi.fn(() => real) } as unknown as typeof FlowRunModule
  return { mod, real }
}

const deps = { fetchPayload: async () => null }

describe('createLazyFlowRunMounter', () => {
  it('does not load the renderer when the transcript has no flow-run card', () => {
    const load = vi.fn()
    const lazy = createLazyFlowRunMounter(deps, load)
    const root = document.createElement('div')
    root.innerHTML = '<p>plain message</p>'
    lazy.mountFlowRuns(root)
    lazy.runEvent({ run_id: 'run_1' })
    lazy.reconnected()
    lazy.destroyAll()
    expect(load).not.toHaveBeenCalled()
  })

  it('loads once on the first placeholder and mounts every root seen meanwhile', async () => {
    const { mod, real } = fakeModule()
    const load = vi.fn(async () => mod)
    const lazy = createLazyFlowRunMounter(deps, load)
    const a = document.createElement('div')
    a.innerHTML = '<div data-flowrun-src="x"></div>'
    const b = document.createElement('div')
    b.innerHTML = '<div data-flowrun-src="y"></div>'
    lazy.mountFlowRuns(a)
    lazy.mountFlowRuns(b)
    await vi.waitFor(() => expect(real.mountFlowRuns).toHaveBeenCalledTimes(2))
    expect(load).toHaveBeenCalledTimes(1)
    expect(mod.createFlowRunMounter).toHaveBeenCalledWith(deps)

    lazy.runEvent({ run_id: 'run_1' })
    lazy.reconnected()
    lazy.mountFlowRuns(document.createElement('div'))
    lazy.destroyAll()
    expect(real.runEvent).toHaveBeenCalledWith({ run_id: 'run_1' })
    expect(real.reconnected).toHaveBeenCalledTimes(1)
    expect(real.mountFlowRuns).toHaveBeenCalledTimes(3)
    expect(real.destroyAll).toHaveBeenCalledTimes(1)
  })

  it('retries the load after a failed chunk fetch', async () => {
    const { mod, real } = fakeModule()
    const diag = vi.fn()
    const load = vi.fn().mockRejectedValueOnce(new Error('chunk')).mockResolvedValue(mod)
    const lazy = createLazyFlowRunMounter({ ...deps, diag }, load)
    const root = document.createElement('div')
    root.innerHTML = '<div data-flowrun-src="x"></div>'
    lazy.mountFlowRuns(root)
    await vi.waitFor(() =>
      expect(diag).toHaveBeenCalledWith('flowrun.load_failed', expect.anything()),
    )
    lazy.mountFlowRuns(root)
    await vi.waitFor(() => expect(real.mountFlowRuns).toHaveBeenCalled())
    expect(load).toHaveBeenCalledTimes(2)
  })
})
