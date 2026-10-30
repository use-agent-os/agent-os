import { describe, expect, it } from 'vitest'
import {
  applySnapshot,
  emptyTracker,
  ingest,
  latestRunId,
  runImages,
  runsNeedingSnapshot,
  runView,
  toFlowEvent,
  type RunTracker,
} from './run-tracker'
import { DOC, IMG } from './test-fixtures'
import type { FlowEvent, RunRecord } from './types'

const R = 'run_1'
const F = DOC.id

const run = (seq: number, status: 'queued' | 'running' | 'done' | 'failed'): FlowEvent => ({
  kind: 'run',
  run_id: R,
  flow_id: F,
  seq,
  status,
})

const node = (
  seq: number,
  node_id: string,
  state: 'queued' | 'running' | 'cached' | 'done' | 'error',
  extra: Partial<FlowEvent> = {},
): FlowEvent =>
  ({ kind: 'node', run_id: R, flow_id: F, seq, node_id, state, ...extra }) as FlowEvent

function feed(events: FlowEvent[], now = 0): RunTracker {
  return events.reduce((tr, ev) => ingest(tr, ev, now), emptyTracker())
}

describe('event ordering by seq', () => {
  it('applies in-order events', () => {
    const tr = feed([run(1, 'queued'), run(2, 'running'), node(3, 'n5', 'running')])
    expect(runView(tr, R)?.status).toBe('running')
    expect(runView(tr, R)?.nodes.n5?.state).toBe('running')
  })

  it('buffers an event that skips ahead until the gap fills, then applies both in order', () => {
    let tr = feed([run(1, 'queued')])
    tr = ingest(tr, node(3, 'n5', 'done', { ms: 900 }), 100)
    expect(runView(tr, R)?.nodes.n5).toBeUndefined()
    expect(runsNeedingSnapshot(tr, 100)).toEqual([])
    expect(runsNeedingSnapshot(tr, 100 + 1500)).toEqual([R])
    tr = ingest(tr, node(2, 'n5', 'running', { progress: { value: 1, max: 4 } }), 200)
    expect(runView(tr, R)?.nodes.n5).toEqual({ state: 'done', ms: 900 })
    expect(runsNeedingSnapshot(tr, 10_000)).toEqual([])
  })

  it('drops duplicates and older events', () => {
    let tr = feed([run(1, 'queued'), run(2, 'running'), node(3, 'n5', 'done')])
    const before = tr
    tr = ingest(tr, node(3, 'n5', 'error'))
    tr = ingest(tr, node(2, 'n5', 'running'))
    expect(tr).toBe(before)
  })

  it('a node never moves backwards, and progress only lives while running', () => {
    const tr = feed([
      run(1, 'running'),
      node(2, 'n5', 'running', { progress: { value: 2, max: 4 } }),
      node(3, 'n5', 'done', { ms: 1200, cost_usd: 0.24 }),
    ])
    expect(runView(tr, R)?.nodes.n5).toEqual({ state: 'done', ms: 1200, cost_usd: 0.24 })
  })

  it('keeps node outputs', () => {
    const tr = feed([
      run(1, 'running'),
      node(2, 'n5', 'done', { outputs: { images: [IMG] } }),
      node(3, 'n4', 'done', { outputs: { text: 'a prompt' } }),
    ])
    expect(runView(tr, R)?.outputs).toEqual({ n5: { images: [IMG] }, n4: { text: 'a prompt' } })
  })

  it('a run joined mid-way asks for a snapshot right away', () => {
    const tr = feed([node(7, 'n5', 'running')])
    expect(runView(tr, R)?.nodes.n5?.state).toBe('running')
    expect(runsNeedingSnapshot(tr, 0)).toEqual([R])
  })
})

describe('snapshots', () => {
  const record: RunRecord = {
    run_id: R,
    flow_id: F,
    rev: 17,
    status: 'running',
    created: '2026-10-29T10:00:00Z',
    preview: null,
    origin: 'canvas',
    error: null,
    image_count: 0,
    nodes: { n4: { state: 'done', ms: 2100 }, n5: { state: 'running' } },
    outputs: { n4: { text: 'prompt' } },
    graph: DOC,
  }

  it('replaces the view and clears the snapshot request', () => {
    let tr = feed([node(7, 'n5', 'running')])
    tr = applySnapshot(tr, record)
    expect(runView(tr, R)?.nodes.n4).toEqual({ state: 'done', ms: 2100 })
    expect(runsNeedingSnapshot(tr, 0)).toEqual([])
    // Events after it continue in order.
    tr = ingest(tr, node(8, 'n5', 'done'))
    expect(runView(tr, R)?.nodes.n5?.state).toBe('done')
  })

  it('replays buffered events past the snapshot without regressing it', () => {
    let tr = feed([run(1, 'running')])
    tr = ingest(tr, node(4, 'n5', 'done'), 0) // gap: 2, 3 missing
    tr = applySnapshot(tr, { ...record, seq: 3 })
    expect(runView(tr, R)?.nodes.n5?.state).toBe('done')
    // A stale event replayed on top cannot undo a settled node.
    tr = applySnapshot(tr, {
      ...record,
      nodes: { n5: { state: 'done' } },
      status: 'done',
    })
    tr = ingest(tr, node(5, 'n5', 'running'))
    expect(runView(tr, R)?.nodes.n5?.state).toBe('done')
    tr = ingest(tr, run(6, 'running'))
    expect(runView(tr, R)?.status).toBe('done')
  })

  it('images of a run skip the ones it was given (load_image)', () => {
    const tr = applySnapshot(emptyTracker(), {
      ...record,
      graph: { ...DOC, nodes: DOC.nodes.filter((n) => n.id !== 'n7') },
      outputs: {
        n6: { images: [{ ...IMG, sha256: 'b'.repeat(64) }] },
        n5: { images: [IMG, IMG] },
      },
    })
    expect(runImages(runView(tr, R))).toEqual([IMG])
  })

  it('new images: Save outputs first, then image nodes that ran; each blob once', () => {
    const edit = { ...IMG, sha256: 'c'.repeat(64) }
    const tr = applySnapshot(emptyTracker(), {
      ...record,
      nodes: { n5: { state: 'done' }, n8: { state: 'done' }, n7: { state: 'done' } },
      graph: {
        ...DOC,
        nodes: [...DOC.nodes, { id: 'n8', type: 'image', pos: [0, 0], values: {} }],
      },
      outputs: {
        n5: { images: [IMG] },
        n8: { images: [edit] }, // an edit not wired to Save: still paid for, still shown
        n7: { images: [IMG] },
      },
    })
    expect(runImages(runView(tr, R))).toEqual([IMG, edit])
  })

  it('an image node served from cache is not a result of this run', () => {
    const edit = { ...IMG, sha256: 'c'.repeat(64) }
    const tr = applySnapshot(emptyTracker(), {
      ...record,
      nodes: { n5: { state: 'cached' }, n8: { state: 'done' } },
      outputs: { n5: { images: [IMG] }, n8: { images: [edit] } },
    })
    expect(runImages(runView(tr, R))).toEqual([edit])
  })

  it('a Save of a cached image did not produce it', () => {
    const tr = applySnapshot(emptyTracker(), {
      ...record,
      nodes: { n5: { state: 'cached' }, n7: { state: 'done' } },
      outputs: { n5: { images: [IMG] }, n7: { images: [IMG] } },
    })
    expect(runImages(runView(tr, R))).toEqual([])
  })

  it("the gateway's own list (RunRecord.images) wins when it is there", () => {
    const edit = { ...IMG, sha256: 'c'.repeat(64) }
    const tr = applySnapshot(emptyTracker(), {
      ...record,
      images: [edit],
      outputs: { n5: { images: [IMG] } },
    })
    expect(runImages(runView(tr, R))).toEqual([edit])
  })

  it('live runs (no graph) learn node types from the canvas', () => {
    let tr = feed([run(1, 'running')])
    tr = ingest(tr, node(2, 'n5', 'done', { outputs: { images: [IMG] } }))
    tr = ingest(tr, node(3, 'n7', 'done', { outputs: { images: [IMG] } }))
    const typeOf = (id: string) => DOC.nodes.find((n) => n.id === id)?.type
    expect(runImages(runView(tr, R), typeOf)).toEqual([IMG])
  })

  it('latest run is the one seen last', () => {
    let tr = feed([run(1, 'done')])
    tr = ingest(tr, { kind: 'run', run_id: 'run_2', flow_id: F, seq: 1, status: 'queued' })
    expect(latestRunId(tr, F)).toBe('run_2')
    expect(latestRunId(tr, 'other')).toBeNull()
  })
})

describe('a run that stops', () => {
  it('cancelled: nothing stays running or queued, even without node events', () => {
    const tr = feed([
      run(1, 'running'),
      node(2, 'n4', 'done', { ms: 900 }),
      node(3, 'n5', 'running', { progress: { value: 1, max: 4 } }),
      node(4, 'n7', 'queued'),
      { kind: 'run', run_id: R, flow_id: F, seq: 5, status: 'cancelled' },
    ])
    const view = runView(tr, R)!
    expect(view.status).toBe('cancelled')
    expect(view.nodes.n4).toEqual({ state: 'done', ms: 900 })
    // In flight at Stop: the engine lets the call finish; not started: skipped.
    expect(view.nodes.n5).toEqual({ state: 'stopping' })
    expect(view.nodes.n7).toEqual({ state: 'skipped' })
  })

  it('a late provider result after Stop is kept as "stopped", never running again', () => {
    let tr = feed([
      run(1, 'running'),
      node(2, 'n5', 'running'),
      { kind: 'run', run_id: R, flow_id: F, seq: 3, status: 'cancelled' },
    ])
    tr = ingest(tr, node(4, 'n5', 'done', { cost_usd: 0.04, outputs: { images: [IMG] } }))
    tr = ingest(tr, node(5, 'n5', 'running'))
    tr = ingest(tr, {
      kind: 'run',
      run_id: R,
      flow_id: F,
      seq: 6,
      status: 'cancelled',
      cost_usd: 0.04,
    })
    const view = runView(tr, R)!
    // The image was made and paid for: shown, counted, and cached for a "keep" re-run.
    expect(view.nodes.n5).toEqual({ state: 'done', stopped: true, cost_usd: 0.04 })
    expect(view.status).toBe('cancelled')
    expect(view.cost_usd).toBe(0.04)
    expect(view.outputs.n5).toEqual({ images: [IMG] })
    expect(runImages(view)).toEqual([IMG])
  })

  it('a call failing after Stop leaves the node stopped and the run cancelled', () => {
    let tr = feed([
      run(1, 'running'),
      node(2, 'n5', 'running'),
      { kind: 'run', run_id: R, flow_id: F, seq: 3, status: 'cancelled' },
    ])
    tr = ingest(tr, node(4, 'n5', 'error', { error: { code: 'x', message: 'timeout' } }))
    expect(runView(tr, R)?.nodes.n5).toEqual({ state: 'skipped', stopped: true })
    expect(runView(tr, R)?.status).toBe('cancelled')
  })

  it('a snapshot after the late result keeps the node "stopped"', () => {
    let tr = feed([
      run(1, 'running'),
      node(2, 'n5', 'running'),
      { kind: 'run', run_id: R, flow_id: F, seq: 3, status: 'cancelled' },
      node(4, 'n5', 'done', { cost_usd: 0.04, outputs: { images: [IMG] } }),
    ])
    tr = applySnapshot(tr, {
      run_id: R,
      flow_id: F,
      rev: 1,
      status: 'cancelled',
      created: '2026-10-29T10:00:00Z',
      preview: IMG,
      origin: 'canvas',
      error: null,
      image_count: 1,
      images: [IMG],
      nodes: { n5: { state: 'done', cost_usd: 0.04 } },
      outputs: { n5: { images: [IMG] } },
    })
    expect(runView(tr, R)?.nodes.n5).toEqual({ state: 'done', cost_usd: 0.04, stopped: true })
  })

  it('a stored record of a stopped run never shows running nodes', () => {
    const tr = applySnapshot(emptyTracker(), {
      run_id: R,
      flow_id: F,
      rev: 1,
      status: 'failed',
      created: '2026-10-29T10:00:00Z',
      preview: null,
      origin: 'canvas',
      error: { code: 'flows.interrupted', message: 'The gateway stopped' },
      image_count: 0,
      nodes: { n5: { state: 'running', progress: { value: 1, max: 2 } }, n7: { state: 'queued' } },
      outputs: {},
    })
    expect(runView(tr, R)?.nodes).toEqual({ n5: { state: 'skipped' }, n7: { state: 'skipped' } })
  })

  it('remembers when each node and the run started, for the elapsed clock', () => {
    let tr = ingest(emptyTracker(), run(1, 'running'), 1000)
    tr = ingest(tr, node(2, 'n5', 'running'), 5000)
    tr = ingest(tr, node(3, 'n5', 'running', { progress: { value: 1, max: 4 } }), 9000)
    const view = runView(tr, R)!
    expect(view.startedAt).toBe(1000)
    expect(view.nodeStartedAt.n5).toBe(5000)
  })
})

describe('event parsing', () => {
  it('accepts well-formed payloads and refuses the rest', () => {
    expect(toFlowEvent('run', { run_id: R, flow_id: F, seq: 1, status: 'queued' })).toMatchObject({
      kind: 'run',
    })
    expect(toFlowEvent('run', { run_id: R, flow_id: F, status: 'queued' })).toBeNull()
    expect(toFlowEvent('node', { run_id: R, flow_id: F, seq: 1, state: 'done' })).toBeNull()
    expect(toFlowEvent('node', null)).toBeNull()
  })
})
