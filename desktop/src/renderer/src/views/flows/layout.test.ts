import { describe, expect, it } from 'vitest'
import { createEditorStore, isDirty } from './editor-store'
import { graphToDoc } from './graph'
import { fitViewportFor } from './FlowEditor'
import { reflowColumns, REFLOW_GAP } from './layout'
import { DOC } from './test-fixtures'
import type { FlowDoc } from './types'

const item = (id: string, x: number, y: number, height: number, auto = true) => ({
  id,
  x,
  y,
  height,
  auto,
})

describe('reflowColumns', () => {
  it('stacks a column by measured heights with a 48px gap, keeping its order', () => {
    // The engine estimated 200px each; the real nodes are 320 and 140 tall.
    const moves = reflowColumns([
      item('a', 40, 80, 320),
      item('b', 40, 340, 140),
      item('c', 40, 600, 90),
    ])
    expect(moves).toEqual({
      b: { x: 40, y: 80 + 320 + REFLOW_GAP },
      c: { x: 40, y: 80 + 320 + 48 + 140 + 48 },
    })
  })

  it('columns are independent, and nothing moves once stacked', () => {
    const stacked = [item('a', 40, 0, 100), item('b', 40, 148, 50), item('c', 400, 10, 500)]
    expect(reflowColumns(stacked)).toEqual({})
  })

  it('never moves a node without auto_pos; it pushes the engine-placed ones below it', () => {
    const moves = reflowColumns([
      item('top', 40, 0, 100),
      item('mine', 40, 120, 300, false),
      item('below', 40, 200, 80),
    ])
    expect(moves.mine).toBeUndefined()
    expect(moves.below).toEqual({ x: 40, y: 120 + 300 + 48 })
  })

  it('waits until every node of the column is measured', () => {
    expect(reflowColumns([item('a', 40, 0, 100), item('b', 40, 120, 0)])).toEqual({})
  })
})

const AUTO: FlowDoc = {
  ...structuredClone(DOC),
  nodes: [
    { id: 'n1', type: 'text', pos: [40, 80], values: {}, auto_pos: true },
    { id: 'n2', type: 'text', pos: [40, 340], values: {}, auto_pos: true },
    { id: 'n3', type: 'image', pos: [400, 40], values: {}, auto_pos: true },
    { id: 'n4', type: 'save', pos: [40, 600], values: {} },
  ],
  links: [],
  inputs: {},
}

function measure(store: ReturnType<typeof createEditorStore>, heights: Record<string, number>) {
  store.getState().onNodesChange(
    Object.entries(heights).map(([id, height]) => ({
      id,
      type: 'dimensions' as const,
      dimensions: { width: 240, height },
    })),
  )
}

describe('editor re-flow of engine-placed nodes (fake measurements)', () => {
  it('re-flows once measured, as the baseline: no undo step, still clean', () => {
    const store = createEditorStore(AUTO)
    measure(store, { n1: 320, n2: 140, n3: 600, n4: 100 })
    store.getState().reflowAuto()
    const pos = Object.fromEntries(store.getState().nodes.map((n) => [n.id, n.position]))
    expect(pos.n2).toEqual({ x: 40, y: 80 + 320 + 48 })
    // n4 (no auto_pos) stays where it is.
    expect(pos.n4).toEqual({ x: 40, y: 600 })
    expect(store.getState().history.past).toHaveLength(0)
    expect(isDirty(store.getState())).toBe(false)
  })

  it('the next save carries the re-flowed positions, without the flag', () => {
    const store = createEditorStore(AUTO)
    measure(store, { n1: 320, n2: 140, n3: 600, n4: 100 })
    store.getState().reflowAuto()
    store.getState().setValue('n3', 'count', 2)
    const s = store.getState()
    const doc = graphToDoc(s.meta, s.history.present)
    expect(doc.nodes.find((n) => n.id === 'n2')?.pos).toEqual([40, 448])
    expect(doc.nodes.every((n) => !('auto_pos' in n))).toBe(true)
    store.getState().markSaved(18, 'k')
    expect(store.getState().nodes.some((n) => n.data.autoPos)).toBe(false)
  })

  it('grows again after a run (taller outputs) while still engine-placed', () => {
    const store = createEditorStore(AUTO)
    measure(store, { n1: 320, n2: 140, n3: 600, n4: 100 })
    store.getState().reflowAuto()
    measure(store, { n1: 520 })
    store.getState().reflowAuto()
    expect(store.getState().nodes.find((n) => n.id === 'n2')?.position).toEqual({
      x: 40,
      y: 80 + 520 + 48,
    })
  })

  it('a node the person drags stops being re-flowed', () => {
    const store = createEditorStore(AUTO)
    store
      .getState()
      .onNodesChange([{ type: 'position', id: 'n2', position: { x: 40, y: 900 }, dragging: true }])
    store
      .getState()
      .onNodesChange([{ type: 'position', id: 'n2', position: { x: 40, y: 900 }, dragging: false }])
    measure(store, { n1: 320, n2: 140, n3: 600, n4: 100 })
    store.getState().reflowAuto()
    expect(store.getState().nodes.find((n) => n.id === 'n2')?.data.autoPos).toBe(false)
    expect(store.getState().nodes.find((n) => n.id === 'n2')?.position).toEqual({ x: 40, y: 900 })
  })

  it('a flow without auto_pos is never moved', () => {
    const store = createEditorStore(DOC)
    measure(store, { n3: 900, n4: 900, n5: 900, n6: 900, n7: 900 })
    const before = store.getState().nodes.map((n) => n.position)
    store.getState().reflowAuto()
    expect(store.getState().nodes.map((n) => n.position)).toEqual(before)
  })
})

describe('Fit', () => {
  it('zooms out as far as needed for a graph larger than the canvas (to 25% at most)', () => {
    const vp = fitViewportFor({ x: 0, y: 0, width: 2600, height: 900 }, 1200, 700)
    expect(vp.zoom).toBeLessThan(0.5)
    expect(vp.zoom).toBeGreaterThan(0.25)
    const huge = fitViewportFor({ x: 0, y: 0, width: 20000, height: 900 }, 1200, 700)
    expect(huge.zoom).toBe(0.25)
  })

  it('never zooms in past 100%', () => {
    expect(fitViewportFor({ x: 0, y: 0, width: 300, height: 200 }, 1200, 700).zoom).toBe(1)
  })
})
