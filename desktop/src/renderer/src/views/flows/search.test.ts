import { describe, expect, it } from 'vitest'
import { accepts, groupedNodes, matchRank, searchNodes } from './search'
import { NODES } from './test-fixtures'
import type { NodeSchema } from './types'

const ids = (list: NodeSchema[]) => list.map((n) => n.id)

describe('add-node menu ranking', () => {
  it('ranks exact > prefix > word start > substring', () => {
    expect(matchRank('Image', 'image')).toBe(0)
    expect(matchRank('Image', 'ima')).toBe(1)
    expect(matchRank('Load Image', 'ima')).toBe(2)
    expect(matchRank('Describe Image', 'scri')).toBe(3)
    expect(matchRank('Save', 'zzz')).toBeNull()
  })

  it('orders results by rank, then group, then name', () => {
    expect(ids(searchNodes(NODES.nodes, 'image'))).toEqual([
      'image', // exact
      'load_image', // word start, input group
      'describe_image', // word start, ai group
    ])
    expect(ids(searchNodes(NODES.nodes, 'text'))).toEqual([
      'text', // exact
      'join_text', // word start
    ])
  })

  it('matches the node id as well as its display name', () => {
    expect(ids(searchNodes(NODES.nodes, 'prompt_w'))).toEqual(['prompt_writer'])
  })

  it('an empty query lists everything in group order', () => {
    expect(groupedNodes(NODES.nodes).map((g) => [g.group, ids(g.nodes)])).toEqual([
      ['input', ['text', 'load_image']],
      ['utility', ['join_text']],
      ['ai', ['prompt_writer', 'describe_image', 'image']],
      ['output', ['save']],
    ])
  })
})

describe('add-node menu type filter', () => {
  it('a link dragged out of an IMAGE output offers nodes with an IMAGE socket', () => {
    const filter = { side: 'input' as const, type: 'IMAGE' }
    expect(ids(searchNodes(NODES.nodes, '', filter))).toEqual(
      expect.arrayContaining(['describe_image', 'image', 'save']),
    )
    expect(ids(searchNodes(NODES.nodes, '', filter))).toHaveLength(3)
  })

  it('a link dragged out of a TEXT input offers nodes with a TEXT output', () => {
    const filter = { side: 'output' as const, type: 'TEXT' }
    expect(ids(groupedNodes(NODES.nodes, filter).flatMap((g) => g.nodes))).toEqual([
      'text',
      'join_text',
      'prompt_writer',
      'describe_image',
    ])
  })

  it('widget-only inputs never count as a socket', () => {
    const text = NODES.nodes.find((n) => n.id === 'text')!
    expect(accepts(text, { side: 'input', type: 'TEXT' })).toBe(false)
  })

  it('filter and query combine', () => {
    expect(ids(searchNodes(NODES.nodes, 'sa', { side: 'input', type: 'IMAGE' }))).toEqual(['save'])
    expect(searchNodes(NODES.nodes, 'save', { side: 'output', type: 'IMAGE' })).toEqual([])
  })
})
