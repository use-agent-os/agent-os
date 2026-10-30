import { describe, expect, it } from 'vitest'
import {
  canvasEdgeToDoc,
  compatibleHandle,
  docToGraph,
  duplicateNodes,
  graphToDoc,
  isValidConnection,
  nextId,
  prunePublicInputs,
  snapshotOf,
  wouldCycle,
  type ConnectionContext,
} from './graph'
import { schemaIndex } from './schema-logic'
import { DOC, NODES } from './test-fixtures'

const schemas = schemaIndex(NODES)

function ctx(doc = DOC): ConnectionContext {
  const g = docToGraph(doc)
  return { nodes: g.nodes, edges: g.edges, schemas }
}

const link = (source: string, sourceHandle: string, target: string, targetHandle: string) => ({
  source,
  sourceHandle,
  target,
  targetHandle,
})

describe('flow document ⇄ canvas graph', () => {
  it('round-trips a document: named values, links by name, link order, public inputs', () => {
    const g = docToGraph(DOC)
    expect(g.nodes.find((n) => n.id === 'n5')?.data.values).toEqual({
      prompt: '',
      model: 'auto',
      aspect: '3:2',
      count: 4,
      variation: 'new',
    })
    expect(g.edges[1]).toMatchObject({
      source: 'n4',
      sourceHandle: 'text',
      target: 'n5',
      targetHandle: 'prompt',
    })
    const doc = graphToDoc(
      { id: DOC.id, name: DOC.name, rev: DOC.rev },
      snapshotOf(g.nodes, g.edges, DOC.inputs),
      DOC.viewport,
    )
    const expected: Partial<typeof DOC> = { ...DOC }
    delete expected.updated
    expect(doc).toEqual(expected)
  })

  it('keeps a title and rounds positions to whole pixels', () => {
    const g = docToGraph(DOC)
    const n = g.nodes.find((x) => x.id === 'n6')!
    n.position = { x: 10.6, y: -3.2 }
    const snap = snapshotOf(g.nodes, g.edges, {})
    expect(snap.nodes.find((x) => x.id === 'n6')).toMatchObject({
      pos: [11, -3],
      title: 'Reference',
    })
  })

  it('serialises an edge by output and input name, never an index', () => {
    expect(
      canvasEdgeToDoc({
        id: 'l9',
        source: 'n1',
        sourceHandle: 'image',
        target: 'n2',
        targetHandle: 'images',
      }),
    ).toEqual({ id: 'l9', from: ['n1', 'image'], to: ['n2', 'images'] })
  })

  it('omits the viewport when there is none', () => {
    const g = docToGraph(DOC)
    const doc = graphToDoc({ id: 'f', name: 'x', rev: 1 }, snapshotOf(g.nodes, g.edges, {}))
    expect('viewport' in doc).toBe(false)
  })

  it('drops public inputs whose node is gone', () => {
    const g = docToGraph(DOC)
    expect(
      prunePublicInputs(
        { idea: 'n3.text', gone: 'n99.text' },
        g.nodes.filter((n) => n.id !== 'n99'),
      ),
    ).toEqual({ idea: 'n3.text' })
    expect(
      prunePublicInputs(
        { idea: 'n3.text' },
        g.nodes.filter((n) => n.id !== 'n3'),
      ),
    ).toEqual({})
  })
})

describe('connection validity', () => {
  const blank = {
    ...DOC,
    links: [],
  }

  it('accepts an exact type match into a socket', () => {
    expect(isValidConnection(link('n3', 'text', 'n5', 'prompt'), ctx(blank))).toBe(true)
    expect(isValidConnection(link('n6', 'image', 'n5', 'images'), ctx(blank))).toBe(true)
  })

  it('refuses a type mismatch', () => {
    expect(isValidConnection(link('n3', 'text', 'n5', 'images'), ctx(blank))).toBe(false)
    expect(isValidConnection(link('n6', 'image', 'n5', 'prompt'), ctx(blank))).toBe(false)
  })

  it('refuses a widget-only input (socket: false) and unknown handles', () => {
    expect(isValidConnection(link('n3', 'text', 'n4', 'style'), ctx(blank))).toBe(false)
    expect(isValidConnection(link('n3', 'nope', 'n4', 'idea'), ctx(blank))).toBe(false)
    expect(isValidConnection(link('n3', 'text', 'n4', 'nope'), ctx(blank))).toBe(false)
  })

  it('allows one link into a non-multi input, several into a multi IMAGE input', () => {
    // n5.prompt already takes n4.text in DOC.
    expect(isValidConnection(link('n3', 'text', 'n5', 'prompt'), ctx())).toBe(false)
    const twoImages = {
      ...DOC,
      nodes: [
        ...DOC.nodes,
        { id: 'n8', type: 'load_image', pos: [0, 0] as [number, number], values: {} },
      ],
    }
    // n5.images already has n6.image; a second source is fine.
    expect(isValidConnection(link('n8', 'image', 'n5', 'images'), ctx(twoImages))).toBe(true)
    // The very same link twice is not.
    expect(isValidConnection(link('n6', 'image', 'n5', 'images'), ctx(twoImages))).toBe(false)
  })

  it('refuses a link that would close a loop, and a self link', () => {
    // n5 → n7 exists; Save has no outputs, so build a loop out of Join Text instead.
    const loop = {
      ...DOC,
      nodes: [
        { id: 'a', type: 'join_text', pos: [0, 0] as [number, number], values: {} },
        { id: 'b', type: 'join_text', pos: [0, 0] as [number, number], values: {} },
      ],
      links: [
        { id: 'l1', from: ['a', 'text'] as [string, string], to: ['b', 'a'] as [string, string] },
      ],
      inputs: {},
    }
    expect(isValidConnection(link('b', 'text', 'a', 'a'), ctx(loop))).toBe(false)
    expect(isValidConnection(link('a', 'text', 'a', 'b'), ctx(loop))).toBe(false)
    expect(isValidConnection(link('a', 'text', 'b', 'b'), ctx(loop))).toBe(true)
    expect(wouldCycle(ctx(loop).edges, 'b', 'a')).toBe(true)
  })
})

describe('ids and duplication', () => {
  it('numbers one past the highest id in use', () => {
    expect(nextId('n', ['n1', 'n7', 'x9', 'n3'])).toBe('n8')
    expect(nextId('l', [])).toBe('l1')
  })

  it('finds the handle a dragged link can land on', () => {
    expect(compatibleHandle(schemas.get('image')!, 'input', 'IMAGE')).toBe('images')
    expect(compatibleHandle(schemas.get('image')!, 'input', 'TEXT')).toBe('prompt')
    expect(compatibleHandle(schemas.get('describe_image')!, 'output', 'TEXT')).toBe('text')
    expect(compatibleHandle(schemas.get('save')!, 'output', 'IMAGE')).toBeNull()
  })

  it('duplicates the picked nodes with the links among them only', () => {
    const g = docToGraph(DOC)
    const picked = g.nodes.filter((n) => n.id === 'n3' || n.id === 'n4')
    const copy = duplicateNodes(picked, g)
    expect(copy.nodes.map((n) => n.id)).toEqual(['n8', 'n9'])
    expect(copy.nodes[0]!.position).toEqual({ x: 60, y: 340 })
    expect(copy.edges).toHaveLength(1)
    expect(copy.edges[0]).toMatchObject({ id: 'l5', source: 'n8', target: 'n9' })
    // Values are copies, not shared objects.
    copy.nodes[0]!.data.values.text = 'changed'
    expect(g.nodes.find((n) => n.id === 'n3')!.data.values.text).toBe('a lighthouse')
  })
})
