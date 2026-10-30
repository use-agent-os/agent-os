import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type EdgeChange,
  type NodeChange,
} from '@xyflow/react'
import { createStore, type StoreApi } from 'zustand/vanilla'
import {
  docToGraph,
  duplicateNodes,
  nextId,
  prunePublicInputs,
  snapshotKey,
  snapshotOf,
  type DocMeta,
  type FlowCanvasEdge,
  type FlowCanvasNode,
  type GraphSnapshot,
} from './graph'
import { canRedo, canUndo, commit, createHistory, redo, reset, undo, type History } from './history'
import type { EstimateResult, FlowDoc, NodeErrors } from './types'
import type { RunView } from './run-tracker'

/**
 * One open flow: the canvas graph, its undo history, what was last saved,
 * and the runtime facts the nodes paint (the run on screen, the estimate,
 * validation errors). A vanilla zustand store per editor, so a commit reads
 * the graph it just wrote and the history logic runs without React.
 */

export interface EditorState {
  meta: DocMeta
  nodes: FlowCanvasNode[]
  edges: FlowCanvasEdge[]
  inputs: Record<string, string>
  history: History<GraphSnapshot>
  /** `docKey` of what the gateway holds (after the last good save or load). */
  savedKey: string
  /** The run whose node states the canvas shows. */
  run: RunView | null
  estimate: EstimateResult | null
  errors: NodeErrors
}

export interface EditorActions {
  onNodesChange: (changes: NodeChange<FlowCanvasNode>[]) => void
  onEdgesChange: (changes: EdgeChange<FlowCanvasEdge>[]) => void
  connect: (conn: Connection) => void
  addNode: (
    type: string,
    position: { x: number; y: number },
    link?: Connection | null,
    values?: Record<string, unknown>,
  ) => string
  setValue: (nodeId: string, name: string, value: unknown, coalesce?: boolean) => void
  /** Several values at once, one undo step (a model change dropping stale combos). */
  patchValues: (nodeId: string, patch: Record<string, unknown>, drop?: string[]) => void
  setTitle: (nodeId: string, title: string | null) => void
  setName: (name: string) => void
  duplicateSelected: () => void
  deleteSelected: () => void
  undo: () => void
  redo: () => void
  /** Another graph as one undoable step (restore from a dropped PNG). */
  replaceGraph: (doc: Pick<FlowDoc, 'nodes' | 'links' | 'inputs'>) => void
  /** The gateway's copy wins: reload it, history starts over. */
  loadDoc: (doc: FlowDoc) => void
  /** A save went through: `rev` is the gateway's new revision, `key` what was sent. */
  markSaved: (rev: number, key: string) => void
  setRun: (run: RunView | null) => void
  setEstimate: (estimate: EstimateResult | null) => void
  setErrors: (errors: NodeErrors) => void
}

export type EditorStore = StoreApi<EditorState & EditorActions>

/** What "saved" compares: the name plus the graph (no viewport). */
export function docKey(name: string, graphKey: string): string {
  return `${name}\u0000${graphKey}`
}

function present(nodes: FlowCanvasNode[], edges: FlowCanvasEdge[], inputs: Record<string, string>) {
  return snapshotOf(nodes, edges, inputs)
}

function stripRuntime(n: FlowCanvasNode): FlowCanvasNode {
  return { ...n, selected: false, dragging: false }
}

export function createEditorStore(doc: FlowDoc): EditorStore {
  const graph = docToGraph(doc)
  const inputs = { ...(doc.inputs ?? {}) }
  const first = present(graph.nodes, graph.edges, inputs)
  const history = createHistory(first, snapshotKey)

  return createStore<EditorState & EditorActions>()((set, get) => {
    /** Record the current graph as a change (no-op when nothing changed). */
    const record = (coalesce?: string) => {
      const s = get()
      const snap = present(s.nodes, s.edges, s.inputs)
      const next = commit(s.history, snap, snapshotKey, { coalesce })
      if (next !== s.history) set({ history: next })
    }

    const restore = (snap: GraphSnapshot) => {
      const g = docToGraph(snap)
      set({ nodes: g.nodes, edges: g.edges, inputs: { ...snap.inputs } })
    }

    return {
      meta: { id: doc.id, name: doc.name, rev: doc.rev },
      nodes: graph.nodes,
      edges: graph.edges,
      inputs,
      history,
      savedKey: docKey(doc.name, history.key),
      run: null,
      estimate: null,
      errors: {},

      onNodesChange: (changes) => {
        const s = get()
        const nodes = applyNodeChanges(changes, s.nodes)
        let edges = s.edges
        let inputsNext = s.inputs
        const removed = new Set(changes.filter((c) => c.type === 'remove').map((c) => c.id))
        if (removed.size) {
          // Links of a deleted node go in the same step, so one delete is one undo.
          edges = edges.filter((e) => !removed.has(e.source) && !removed.has(e.target))
          inputsNext = prunePublicInputs(s.inputs, nodes)
        }
        // A move is committed once, when the drag lets go.
        const dragEnded = changes.some((c) => c.type === 'position' && c.dragging === false)
        set({ nodes, edges, inputs: inputsNext })
        if (removed.size || dragEnded) record()
      },

      onEdgesChange: (changes) => {
        const s = get()
        const edges = applyEdgeChanges(changes, s.edges)
        set({ edges })
        if (changes.some((c) => c.type === 'remove')) record()
      },

      connect: (conn) => {
        const s = get()
        const id = nextId(
          'l',
          s.edges.map((e) => e.id),
        )
        set({ edges: addEdge({ ...conn, id }, s.edges) })
        record()
      },

      addNode: (type, position, link, values) => {
        const s = get()
        const id = nextId(
          'n',
          s.nodes.map((n) => n.id),
        )
        const node: FlowCanvasNode = {
          id,
          type: 'flow',
          position: { x: Math.round(position.x), y: Math.round(position.y) },
          data: { type, title: null, values: { ...(values ?? {}) } },
          selected: true,
        }
        let edges = s.edges
        if (link) {
          const conn = {
            ...link,
            source: link.source || id,
            target: link.target || id,
          }
          edges = addEdge(
            {
              ...conn,
              id: nextId(
                'l',
                edges.map((e) => e.id),
              ),
            },
            edges,
          )
        }
        set({
          nodes: [...s.nodes.map((n) => (n.selected ? { ...n, selected: false } : n)), node],
          edges,
        })
        record()
        return id
      },

      setValue: (nodeId, name, value, coalesce = false) => {
        const s = get()
        set({
          nodes: s.nodes.map((n) =>
            n.id === nodeId
              ? { ...n, data: { ...n.data, values: { ...n.data.values, [name]: value } } }
              : n,
          ),
        })
        record(coalesce ? `value:${nodeId}:${name}` : undefined)
      },

      patchValues: (nodeId, patch, drop = []) => {
        const s = get()
        set({
          nodes: s.nodes.map((n) => {
            if (n.id !== nodeId) return n
            const values = { ...n.data.values, ...patch }
            for (const key of drop) delete values[key]
            return { ...n, data: { ...n.data, values } }
          }),
        })
        record()
      },

      setTitle: (nodeId, title) => {
        const s = get()
        const clean = title?.trim() ? title.trim() : null
        set({
          nodes: s.nodes.map((n) =>
            n.id === nodeId ? { ...n, data: { ...n.data, title: clean } } : n,
          ),
        })
        record()
      },

      setName: (name) => {
        const clean = name.trim()
        if (!clean) return
        set({ meta: { ...get().meta, name: clean } })
      },

      duplicateSelected: () => {
        const s = get()
        const picked = s.nodes.filter((n) => n.selected)
        if (!picked.length) return
        const copy = duplicateNodes(picked, { nodes: s.nodes, edges: s.edges })
        set({
          nodes: [...s.nodes.map((n) => ({ ...n, selected: false })), ...copy.nodes],
          edges: [...s.edges, ...copy.edges],
        })
        record()
      },

      deleteSelected: () => {
        const s = get()
        const nodeIds = new Set(s.nodes.filter((n) => n.selected).map((n) => n.id))
        const edgeIds = new Set(s.edges.filter((e) => e.selected).map((e) => e.id))
        if (!nodeIds.size && !edgeIds.size) return
        const nodes = s.nodes.filter((n) => !nodeIds.has(n.id))
        set({
          nodes,
          edges: s.edges.filter(
            (e) => !edgeIds.has(e.id) && !nodeIds.has(e.source) && !nodeIds.has(e.target),
          ),
          inputs: prunePublicInputs(s.inputs, nodes),
        })
        record()
      },

      undo: () => {
        const s = get()
        if (!canUndo(s.history)) return
        const h = undo(s.history, snapshotKey)
        set({ history: h })
        restore(h.present)
      },

      redo: () => {
        const s = get()
        if (!canRedo(s.history)) return
        const h = redo(s.history, snapshotKey)
        set({ history: h })
        restore(h.present)
      },

      replaceGraph: (next) => {
        const g = docToGraph(next)
        set({ nodes: g.nodes, edges: g.edges, inputs: { ...(next.inputs ?? {}) }, errors: {} })
        record()
      },

      loadDoc: (next) => {
        const g = docToGraph(next)
        const nextInputs = { ...(next.inputs ?? {}) }
        const h = reset(present(g.nodes, g.edges, nextInputs), snapshotKey)
        set({
          meta: { id: next.id, name: next.name, rev: next.rev },
          nodes: g.nodes.map(stripRuntime),
          edges: g.edges,
          inputs: nextInputs,
          history: h,
          savedKey: docKey(next.name, h.key),
          errors: {},
        })
      },

      markSaved: (rev, key) => {
        set({ meta: { ...get().meta, rev }, savedKey: key })
      },

      setRun: (run) => set({ run }),
      setEstimate: (estimate) => set({ estimate }),
      setErrors: (errors) => set({ errors }),
    }
  })
}

/** The key the editor would save now. */
export function currentDocKey(s: EditorState): string {
  return docKey(s.meta.name, s.history.key)
}

export function isDirty(s: EditorState): boolean {
  return currentDocKey(s) !== s.savedKey
}
