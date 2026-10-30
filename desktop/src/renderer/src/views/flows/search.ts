import { hasSocket } from './schema-logic'
import type { LinkType, NodeGroup, NodeSchema } from './types'

/**
 * The add-node menu's filter and ranking. A link dragged onto empty canvas
 * narrows the list to nodes that can take its other end; a query ranks what
 * is left: exact name > prefix > word start > substring (case-insensitive),
 * ties by group then name.
 */

export const GROUP_ORDER: readonly NodeGroup[] = ['input', 'utility', 'ai', 'output']

/** What the new node must offer: an input or an output of `type`. */
export interface TypeFilter {
  side: 'input' | 'output'
  type: LinkType
}

export function accepts(schema: NodeSchema, filter: TypeFilter | null): boolean {
  if (!filter) return true
  if (filter.side === 'input') {
    return schema.inputs.some((i) => hasSocket(i) && i.type === filter.type)
  }
  return schema.outputs.some((o) => o.type === filter.type)
}

/** 0 exact, 1 prefix, 2 word start, 3 substring; null = no match. */
export function matchRank(text: string, query: string): number | null {
  const hay = text.toLowerCase().trim()
  const q = query.toLowerCase().trim()
  if (!q) return 0
  if (hay === q) return 0
  if (hay.startsWith(q)) return 1
  if (hay.split(/[\s_\-·/]+/).some((w) => w.startsWith(q))) return 2
  if (hay.includes(q)) return 3
  return null
}

function bestRank(schema: NodeSchema, query: string): number | null {
  const ranks = [matchRank(schema.display, query), matchRank(schema.id, query)].filter(
    (r): r is number => r !== null,
  )
  return ranks.length ? Math.min(...ranks) : null
}

function groupIndex(g: NodeGroup): number {
  const i = GROUP_ORDER.indexOf(g)
  return i === -1 ? GROUP_ORDER.length : i
}

export function searchNodes(
  schemas: readonly NodeSchema[],
  query: string,
  filter: TypeFilter | null = null,
): NodeSchema[] {
  const scored: Array<{ schema: NodeSchema; rank: number }> = []
  for (const schema of schemas) {
    if (!accepts(schema, filter)) continue
    const rank = bestRank(schema, query)
    if (rank === null) continue
    scored.push({ schema, rank })
  }
  scored.sort(
    (a, b) =>
      a.rank - b.rank ||
      groupIndex(a.schema.group) - groupIndex(b.schema.group) ||
      a.schema.display.localeCompare(b.schema.display),
  )
  return scored.map((s) => s.schema)
}

/** For the empty query: the filtered list in group sections, menu order. */
export function groupedNodes(
  schemas: readonly NodeSchema[],
  filter: TypeFilter | null = null,
): Array<{ group: NodeGroup; nodes: NodeSchema[] }> {
  const out: Array<{ group: NodeGroup; nodes: NodeSchema[] }> = []
  for (const group of GROUP_ORDER) {
    const nodes = schemas.filter((s) => s.group === group && accepts(s, filter))
    if (nodes.length) out.push({ group, nodes })
  }
  return out
}
