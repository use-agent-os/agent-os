import type { Edge, Node, Viewport as XyViewport } from '@xyflow/react'
import { hasSocket, inputSpec, outputSpec } from './schema-logic'
import type { FlowDoc, FlowLinkDoc, FlowNodeDoc, LinkType, NodeSchema } from './types'

/**
 * The canvas graph ⇄ the flow document (contract §2).
 *
 * On the canvas a node is an xyflow node whose `data` carries the node type,
 * its optional title and its named widget values; a link is an edge whose
 * handles are the output and input *names* (never indexes). Edge order is
 * link order, which is what a `multi` input concatenates by.
 */

export interface FlowNodeData extends Record<string, unknown> {
  type: string
  title: string | null
  values: Record<string, unknown>
}

export type FlowCanvasNode = Node<FlowNodeData, 'flow'>
export type FlowCanvasEdge = Edge

export interface CanvasGraph {
  nodes: FlowCanvasNode[]
  edges: FlowCanvasEdge[]
}

/** Everything undo restores and "edited" compares: no viewport, no selection. */
export interface GraphSnapshot {
  nodes: FlowNodeDoc[]
  links: FlowLinkDoc[]
  inputs: Record<string, string>
}

export function docNodeToCanvas(n: FlowNodeDoc): FlowCanvasNode {
  return {
    id: n.id,
    type: 'flow',
    position: { x: n.pos?.[0] ?? 0, y: n.pos?.[1] ?? 0 },
    data: { type: n.type, title: n.title ?? null, values: { ...(n.values ?? {}) } },
  }
}

export function docLinkToCanvas(l: FlowLinkDoc): FlowCanvasEdge {
  return {
    id: l.id,
    source: l.from[0],
    sourceHandle: l.from[1],
    target: l.to[0],
    targetHandle: l.to[1],
  }
}

export function docToGraph(doc: Pick<FlowDoc, 'nodes' | 'links'>): CanvasGraph {
  return {
    nodes: (doc.nodes ?? []).map(docNodeToCanvas),
    edges: (doc.links ?? []).map(docLinkToCanvas),
  }
}

function round(n: number): number {
  return Math.round(n)
}

export function canvasNodeToDoc(n: FlowCanvasNode): FlowNodeDoc {
  return {
    id: n.id,
    type: n.data.type,
    pos: [round(n.position.x), round(n.position.y)],
    title: n.data.title ?? null,
    values: { ...n.data.values },
  }
}

export function canvasEdgeToDoc(e: FlowCanvasEdge): FlowLinkDoc {
  return {
    id: e.id,
    from: [e.source, e.sourceHandle ?? ''],
    to: [e.target, e.targetHandle ?? ''],
  }
}

export function snapshotOf(
  nodes: readonly FlowCanvasNode[],
  edges: readonly FlowCanvasEdge[],
  inputs: Record<string, string>,
): GraphSnapshot {
  return {
    nodes: nodes.map(canvasNodeToDoc),
    links: edges.map(canvasEdgeToDoc),
    inputs: { ...inputs },
  }
}

/** Stable comparison key for a snapshot (key order is fixed by the builders above). */
export function snapshotKey(s: GraphSnapshot): string {
  return JSON.stringify(s)
}

export interface DocMeta {
  id: string
  name: string
  rev: number
}

export function graphToDoc(
  meta: DocMeta,
  snapshot: GraphSnapshot,
  viewport?: XyViewport | null,
): FlowDoc {
  const doc: FlowDoc = {
    version: 1,
    id: meta.id,
    name: meta.name,
    rev: meta.rev,
    nodes: snapshot.nodes,
    links: snapshot.links,
    inputs: snapshot.inputs,
  }
  if (viewport) {
    doc.viewport = {
      x: round(viewport.x),
      y: round(viewport.y),
      zoom: Math.round(viewport.zoom * 1000) / 1000,
    }
  }
  return doc
}

/**
 * Public inputs that still point at an input that exists. Deleting a node
 * must not leave the agent a name that resolves to nothing.
 */
export function prunePublicInputs(
  inputs: Record<string, string>,
  nodes: readonly FlowCanvasNode[],
): Record<string, string> {
  const ids = new Set(nodes.map((n) => n.id))
  const next: Record<string, string> = {}
  for (const [name, target] of Object.entries(inputs)) {
    const nodeId = target.split('.')[0] ?? ''
    if (ids.has(nodeId)) next[name] = target
  }
  return next
}

// ── Ids ─────────────────────────────────────────────────────────────────────

/** Next `n<k>` / `l<k>` id: one past the highest numbered id already used. */
export function nextId(prefix: string, taken: Iterable<string>): string {
  let max = 0
  const pattern = new RegExp(`^${prefix}(\\d+)$`)
  for (const id of taken) {
    const m = pattern.exec(id)
    if (m) max = Math.max(max, Number(m[1]))
  }
  return `${prefix}${max + 1}`
}

// ── Connections ─────────────────────────────────────────────────────────────

export interface ConnectionContext {
  nodes: readonly FlowCanvasNode[]
  edges: readonly FlowCanvasEdge[]
  schemas: ReadonlyMap<string, NodeSchema>
}

function schemaOf(ctx: ConnectionContext, nodeId: string): NodeSchema | undefined {
  const node = ctx.nodes.find((n) => n.id === nodeId)
  return node ? ctx.schemas.get(node.data.type) : undefined
}

/** Type carried by a node's output, or null. */
export function outputType(
  ctx: ConnectionContext,
  nodeId: string,
  output: string | null | undefined,
): LinkType | null {
  if (!output) return null
  return outputSpec(schemaOf(ctx, nodeId), output)?.type ?? null
}

/** Type a node's input accepts on its socket, or null when it has none. */
export function inputType(
  ctx: ConnectionContext,
  nodeId: string,
  input: string | null | undefined,
): LinkType | null {
  if (!input) return null
  const spec = inputSpec(schemaOf(ctx, nodeId), input)
  if (!spec || !hasSocket(spec)) return null
  return spec.type ?? null
}

/** True when `from` is reachable from `to` along links: linking from → to would close a loop. */
export function wouldCycle(edges: readonly FlowCanvasEdge[], from: string, to: string): boolean {
  if (from === to) return true
  const next = new Map<string, string[]>()
  for (const e of edges) {
    const list = next.get(e.source) ?? []
    list.push(e.target)
    next.set(e.source, list)
  }
  const seen = new Set<string>()
  const stack = [to]
  while (stack.length) {
    const id = stack.pop()!
    if (id === from) return true
    if (seen.has(id)) continue
    seen.add(id)
    stack.push(...(next.get(id) ?? []))
  }
  return false
}

/**
 * Can this link exist? Exact type match between the output and the input's
 * socket, one link per non-`multi` input, no duplicate link, no cycle.
 */
export function isValidConnection(
  conn: {
    source: string
    target: string
    sourceHandle?: string | null
    targetHandle?: string | null
  },
  ctx: ConnectionContext,
): boolean {
  const { source, target, sourceHandle, targetHandle } = conn
  if (!source || !target || !sourceHandle || !targetHandle) return false
  const from = outputType(ctx, source, sourceHandle)
  const to = inputType(ctx, target, targetHandle)
  if (!from || !to || from !== to) return false
  const spec = inputSpec(schemaOf(ctx, target), targetHandle)
  const into = ctx.edges.filter((e) => e.target === target && e.targetHandle === targetHandle)
  if (into.some((e) => e.source === source && e.sourceHandle === sourceHandle)) return false
  if (!spec?.multi && into.length > 0) return false
  return !wouldCycle(ctx.edges, source, target)
}

/** Links into one input of one node, in link order. */
export function linksInto(
  edges: readonly FlowCanvasEdge[],
  nodeId: string,
  input: string,
): FlowCanvasEdge[] {
  return edges.filter((e) => e.target === nodeId && e.targetHandle === input)
}

/**
 * The first handle on `schema` that can take the other end of a dragged link:
 * for a link dragged out of an output, an input with a socket of that type
 * (free, or `multi`); for one dragged out of an input, an output of that type.
 */
export function compatibleHandle(
  schema: NodeSchema,
  side: 'input' | 'output',
  type: LinkType,
): string | null {
  if (side === 'input') {
    const spec = schema.inputs.find((i) => hasSocket(i) && i.type === type)
    return spec?.name ?? null
  }
  return schema.outputs.find((o) => o.type === type)?.name ?? null
}

/** Copies of the given nodes (new ids, nudged), with the links among them. */
export function duplicateNodes(
  picked: readonly FlowCanvasNode[],
  graph: CanvasGraph,
  offset = 40,
): CanvasGraph {
  const takenNodes = graph.nodes.map((n) => n.id)
  const takenEdges = graph.edges.map((e) => e.id)
  const map = new Map<string, string>()
  const nodes: FlowCanvasNode[] = []
  for (const n of picked) {
    const id = nextId('n', takenNodes)
    takenNodes.push(id)
    map.set(n.id, id)
    nodes.push({
      id,
      type: 'flow',
      position: { x: n.position.x + offset, y: n.position.y + offset },
      data: {
        type: n.data.type,
        title: n.data.title,
        values: structuredClone(n.data.values),
      },
      selected: true,
    })
  }
  const edges: FlowCanvasEdge[] = []
  for (const e of graph.edges) {
    const source = map.get(e.source)
    const target = map.get(e.target)
    if (!source || !target) continue
    const id = nextId('l', takenEdges)
    takenEdges.push(id)
    edges.push({ ...e, id, source, target, selected: false })
  }
  return { nodes, edges }
}
