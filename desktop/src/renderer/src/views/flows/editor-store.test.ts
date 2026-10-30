import { describe, expect, it } from 'vitest'
import { createEditorStore, currentDocKey, isDirty } from './editor-store'
import { commit, createHistory, HISTORY_LIMIT, redo, undo } from './history'
import { DOC } from './test-fixtures'

const key = (n: number) => String(n)

describe('history', () => {
  it('pushes a change, ignores a non-change, caps the stack at 50', () => {
    let h = createHistory<number>(0, key)
    h = commit(h, 0, key)
    expect(h.past).toHaveLength(0)
    for (let i = 1; i <= HISTORY_LIMIT + 10; i++) h = commit(h, i, key, { now: i * 5000 })
    expect(h.past).toHaveLength(HISTORY_LIMIT)
    expect(h.past[0]).toBe(10)
    expect(h.present).toBe(HISTORY_LIMIT + 10)
  })

  it('undo then redo walks back and forth; a new change drops the redo branch', () => {
    let h = createHistory<number>(0, key)
    h = commit(h, 1, key, { now: 1 })
    h = commit(h, 2, key, { now: 10_000 })
    h = undo(h, key)
    expect(h.present).toBe(1)
    h = redo(h, key)
    expect(h.present).toBe(2)
    h = undo(h, key)
    h = commit(h, 3, key, { now: 20_000 })
    expect(h.future).toHaveLength(0)
    expect(h.past).toEqual([0, 1])
  })

  it('coalesces typing into one step within the window', () => {
    let h = createHistory<string>('', (s: string) => s)
    h = commit(h, 'a', (s) => s, { coalesce: 'v', now: 1000 })
    h = commit(h, 'ab', (s) => s, { coalesce: 'v', now: 1500 })
    h = commit(h, 'abc', (s) => s, { coalesce: 'v', now: 2000 })
    expect(h.past).toEqual([''])
    // A pause starts a new step.
    h = commit(h, 'abcd', (s) => s, { coalesce: 'v', now: 9000 })
    expect(h.past).toEqual(['', 'abc'])
    // Another widget is a new step too.
    h = commit(h, 'abcde', (s) => s, { coalesce: 'w', now: 9100 })
    expect(h.past).toEqual(['', 'abc', 'abcd'])
  })
})

describe('editor store undo snapshots', () => {
  it('starts clean, gets dirty on a change, clean again after undo', () => {
    const store = createEditorStore(DOC)
    expect(isDirty(store.getState())).toBe(false)
    store.getState().setValue('n5', 'count', 2)
    expect(isDirty(store.getState())).toBe(true)
    store.getState().undo()
    expect(store.getState().nodes.find((n) => n.id === 'n5')!.data.values.count).toBe(4)
    expect(isDirty(store.getState())).toBe(false)
    store.getState().redo()
    expect(store.getState().nodes.find((n) => n.id === 'n5')!.data.values.count).toBe(2)
  })

  it('selection and in-progress drags are not undo steps; a finished drag is one', () => {
    const store = createEditorStore(DOC)
    const s = store.getState
    s().onNodesChange([{ type: 'select', id: 'n5', selected: true }])
    s().onNodesChange([{ type: 'position', id: 'n5', position: { x: 900, y: 30 }, dragging: true }])
    s().onNodesChange([{ type: 'position', id: 'n5', position: { x: 950, y: 40 }, dragging: true }])
    expect(s().history.past).toHaveLength(0)
    s().onNodesChange([
      { type: 'position', id: 'n5', position: { x: 960, y: 40 }, dragging: false },
    ])
    expect(s().history.past).toHaveLength(1)
    s().undo()
    expect(s().nodes.find((n) => n.id === 'n5')!.position).toEqual({ x: 880, y: 20 })
  })

  it('deleting a node takes its links and public inputs in the same single step', () => {
    const store = createEditorStore(DOC)
    const s = store.getState
    s().onNodesChange([{ type: 'remove', id: 'n3' }])
    // xyflow follows with the edge removal; it must not be a second step.
    s().onEdgesChange([{ type: 'remove', id: 'l1' }])
    expect(s().history.past).toHaveLength(1)
    expect(s().edges.some((e) => e.source === 'n3')).toBe(false)
    expect(s().inputs).toEqual({})
    s().undo()
    expect(s().nodes.some((n) => n.id === 'n3')).toBe(true)
    expect(s().edges.find((e) => e.id === 'l1')).toBeTruthy()
    expect(s().inputs).toEqual({ idea: 'n3.text' })
  })

  it('a dragged-out link plus its new node is one step, auto-connected', () => {
    const store = createEditorStore({ ...DOC, links: [] })
    const s = store.getState
    const id = s().addNode(
      'image',
      { x: 10, y: 10 },
      { source: 'n3', sourceHandle: 'text', target: '', targetHandle: 'prompt' },
    )
    expect(id).toBe('n8')
    expect(s().edges).toEqual([
      expect.objectContaining({ source: 'n3', target: 'n8', targetHandle: 'prompt' }),
    ])
    expect(s().history.past).toHaveLength(1)
  })

  it('typing into one widget is one step; the viewport is never part of it', () => {
    const store = createEditorStore(DOC)
    const s = store.getState
    s().setValue('n3', 'text', 'a l', true)
    s().setValue('n3', 'text', 'a li', true)
    s().setValue('n3', 'text', 'a lig', true)
    expect(s().history.past).toHaveLength(1)
    expect(JSON.stringify(s().history.present)).not.toContain('viewport')
  })

  it('a reload resets history and the saved mark; markSaved moves rev', () => {
    const store = createEditorStore(DOC)
    const s = store.getState
    s().setValue('n5', 'count', 3)
    const sent = currentDocKey(s())
    s().markSaved(18, sent)
    expect(s().meta.rev).toBe(18)
    expect(isDirty(s())).toBe(false)
    s().loadDoc({ ...DOC, rev: 20, name: 'Elsewhere' })
    expect(s().history.past).toHaveLength(0)
    expect(s().meta).toEqual({ id: DOC.id, name: 'Elsewhere', rev: 20 })
    expect(isDirty(s())).toBe(false)
  })

  it('renaming makes the flow dirty but is not an undo step', () => {
    const store = createEditorStore(DOC)
    store.getState().setName('New name')
    expect(isDirty(store.getState())).toBe(true)
    expect(store.getState().history.past).toHaveLength(0)
  })

  it('duplicate selects the copies and is undoable', () => {
    const store = createEditorStore(DOC)
    const s = store.getState
    s().onNodesChange([{ type: 'select', id: 'n3', selected: true }])
    s().duplicateSelected()
    expect(
      s()
        .nodes.filter((n) => n.selected)
        .map((n) => n.id),
    ).toEqual(['n8'])
    s().undo()
    expect(s().nodes.some((n) => n.id === 'n8')).toBe(false)
  })
})
