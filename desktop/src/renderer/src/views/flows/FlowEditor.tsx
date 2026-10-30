import '@xyflow/react/dist/base.css'
import './flows.css'
import {
  Background,
  BackgroundVariant,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  useViewport,
  type Connection,
  type Edge,
  type NodeTypes,
  type OnConnectEnd,
  type OnConnectStart,
} from '@xyflow/react'
import { useQueryClient } from '@tanstack/react-query'
import {
  CloudOff,
  FileWarning,
  Maximize,
  Minus,
  Plus,
  Redo2,
  ShieldAlert,
  Undo2,
} from 'lucide-react'
import { useCallback, useEffect, useId, useMemo, useRef, useState, type DragEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { toast } from 'sonner'
import { useStore } from 'zustand'
import { useKeyboardShortcut } from '@/components/KeyboardShortcuts'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '~/components/ui/button'
import { t, type MessageKey } from '~/i18n'
import { useGateway } from '~/stores/gateway'
import {
  errorText,
  FLOW_ERRORS,
  FLOWS_KEYS,
  flowsErrorCode,
  nodeErrorsOf,
  useFlowChanged,
  useFlowDoc,
  useFlowModels,
  useFlowNodes,
  useFlowRunTracker,
  useFlowRuns,
  useFlowsApi,
  useFlowsInvalidation,
  docWithAccess,
  type FlowsApi,
} from '~/stores/flows'
import { AddNodeMenu } from './AddNodeMenu'
import type { Box } from './Floating'
import { EditorEnvContext, typeColor, type EditorEnv } from './editor-context'
import { createEditorStore, currentDocKey, isDirty, type EditorStore } from './editor-store'
import { droppedImageMime, fileToBase64, MAX_IMAGE_BYTES } from './files'
import { FlowNode } from './FlowNode'
import {
  compatibleHandle,
  graphToDoc,
  isValidConnection,
  outputType,
  type FlowCanvasEdge,
  type FlowCanvasNode,
} from './graph'
import { ImageViewer, type ViewerSource } from './ImageViewer'
import { ModelPicker } from './ModelPicker'
import { estimateChip } from './price'
import { ResultsTray, trayRuns } from './ResultsTray'
import { isTerminal, runView } from './run-tracker'
import { schemaIndex } from './schema-logic'
import type { TypeFilter } from './search'
import type { FlowDoc, ImageRef, ModelsPayload, NodeSchema, NodesPayload } from './types'

const NODE_TYPES: NodeTypes = { flow: FlowNode }

/** `#/flows/:id`: waits for the gateway, then opens the flow. */
export function FlowEditorRoute() {
  const gatewayState = useGateway((s) => s.status.state)
  const { id: rawId } = useParams()
  const id = rawId ? decodeURIComponent(rawId) : ''
  return <FlowEditorLoader key={id} id={id} gatewayState={gatewayState} />
}

function GatewayDown({ gatewayState }: { gatewayState: string }) {
  return (
    <div className="flw-offline">
      <p className="flw-offline__title">
        {gatewayState === 'starting'
          ? t('chat.waitingGateway')
          : t(`gateway.state.${gatewayState}` as MessageKey)}
      </p>
      <p>{t('chat.gatewayDown')}</p>
    </div>
  )
}

function FlowEditorLoader({ id, gatewayState }: { id: string; gatewayState: string }) {
  useFlowsInvalidation()
  const doc = useFlowDoc(id)
  const nodes = useFlowNodes()
  const models = useFlowModels()

  // Once open, the editor stays mounted: a gateway restart, a reconnect or a
  // refetch must not remount it (that re-read the document, re-ran the first
  // fit and lost the view the person had).
  const [opened, setOpened] = useState<{ doc: FlowDoc; nodes: NodesPayload } | null>(null)
  if (!opened && doc.data && nodes.data) setOpened({ doc: doc.data, nodes: nodes.data })

  if (opened) {
    return (
      <ReactFlowProvider>
        <Editor
          doc={opened.doc}
          nodesPayload={nodes.data ?? opened.nodes}
          models={models.data}
          offline={gatewayState !== 'running'}
        />
      </ReactFlowProvider>
    )
  }
  if (gatewayState !== 'running') return <GatewayDown gatewayState={gatewayState} />
  if (doc.isError || nodes.isError) {
    const missing = flowsErrorCode(doc.error) === FLOW_ERRORS.notFound
    return (
      <div className="flw-offline">
        <FileWarning className="size-8 text-dim" strokeWidth={1.25} aria-hidden />
        <p className="flw-offline__title">
          {missing ? t('flows.offline.missing') : t('flows.offline.loadFailed')}
        </p>
        <p>{missing ? t('flows.offline.missing.body') : errorText(doc.error ?? nodes.error)}</p>
        <Link to="/flows" className="mac-button mt-2">
          {t('flows.offline.back')}
        </Link>
      </div>
    )
  }
  return (
    <div className="flw-offline" aria-busy>
      <p>{t('flows.offline.loading')}</p>
    </div>
  )
}

/** The one fit an editor does by itself (a flow with no saved view); ⌘0 / Fit use it too. */
const FIT_OPTIONS = { padding: 0.2, maxZoom: 1 } as const
const DELETE_KEYS = ['Backspace', 'Delete']

interface AddMenuState {
  at: { x: number; y: number }
  /** Where the node goes on the canvas. */
  flowAt: { x: number; y: number }
  filter: TypeFilter | null
  /** The dragged link's fixed end, to connect the new node to. */
  from: { nodeId: string; handle: string; side: 'source' | 'target' } | null
}

interface RestoreState {
  image: ImageRef
  flow: FlowDoc
  at: { x: number; y: number }
}

function Editor({
  doc,
  nodesPayload,
  models,
  offline,
}: {
  doc: FlowDoc
  nodesPayload: NodesPayload
  models: ModelsPayload | undefined
  offline: boolean
}) {
  const [store] = useState(() => createEditorStore(doc))
  const api = useFlowsApi()
  const rf = useReactFlow<FlowCanvasNode, FlowCanvasEdge>()
  const navigate = useNavigate()
  const schemas = useMemo(() => schemaIndex(nodesPayload), [nodesPayload])
  const flowId = doc.id

  const [addMenu, setAddMenu] = useState<AddMenuState | null>(null)
  const [picker, setPicker] = useState<{ nodeId: string; anchor: Box } | null>(null)
  const [viewer, setViewer] = useState<ViewerSource | null>(null)
  const [restore, setRestore] = useState<RestoreState | null>(null)
  const [dragType, setDragType] = useState<string | null>(null)
  const [dropping, setDropping] = useState(false)
  const canvasRef = useRef<HTMLDivElement>(null)
  /** The canvas box, read when a panel opens: panels stay inside it. */
  const [canvasBox, setCanvasBox] = useState<Box | null>(null)
  const readCanvasBox = () => {
    const r = canvasRef.current?.getBoundingClientRect()
    setCanvasBox(r ? { left: r.left, top: r.top, right: r.right, bottom: r.bottom } : null)
  }
  /** Set when the gateway refuses this window's writes: the gateway's own words. */
  // Known up front from `flows.get` (writable / read_only_reason); a refusal
  // later (the gateway changed its mind) lands here too.
  const [readOnly, setReadOnly] = useState<string | null>(
    doc.writable === false ? (doc.read_only_reason ?? '') : null,
  )
  const toldReadOnly = useRef(doc.writable === false)
  const markReadOnly = useCallback((err: unknown) => {
    const detail = errorText(err)
    setReadOnly(detail)
    // One toast per editor, whatever refused first (save, run, stop).
    if (toldReadOnly.current) return
    toldReadOnly.current = true
    toast.error(t('flows.readOnly.title'), {
      id: 'flows-read-only',
      description: detail || t('flows.readOnly.body'),
    })
  }, [])
  const editable = readOnly === null

  const uploadImage = useCallback(
    async (nodeId: string, input: string, file: File) => {
      try {
        const res = await api.upload(file.name, file.type || 'image/png', await fileToBase64(file))
        store.getState().setValue(nodeId, input, res.image)
      } catch (err) {
        toast.error(t('flows.widget.uploadFailed'), { description: errorText(err) })
      }
    },
    [api, store],
  )

  const env: EditorEnv = useMemo(
    () => ({
      store,
      schemas,
      nodes: nodesPayload,
      models,
      openModelPicker: (nodeId, anchor) => {
        readCanvasBox()
        setPicker({
          nodeId,
          anchor: {
            left: anchor.left,
            top: anchor.top,
            right: anchor.right,
            bottom: anchor.bottom,
          },
        })
      },
      openImages: (images, index) => setViewer({ images, index }),
      uploadImage,
      readOnly: !editable,
    }),
    [store, schemas, nodesPayload, models, uploadImage, editable],
  )

  // ── Runs ──────────────────────────────────────────────────────────────────
  const runs = useFlowRuns(flowId)
  const summaries = useMemo(() => runs.data ?? [], [runs.data])
  const { tracker, latestRunId } = useFlowRunTracker(flowId, summaries[0]?.run_id ?? null)
  const [started, setStarted] = useState<{ runId: string; at: number } | null>(null)
  const shownRunId =
    started && !tracker.runs[started.runId]
      ? started.runId
      : (latestRunId ?? started?.runId ?? summaries[0]?.run_id ?? null)
  const shownRun = runView(tracker, shownRunId)
  const running = Boolean(
    shownRunId && (shownRun ? !isTerminal(shownRun.status) : started?.runId === shownRunId),
  )
  useEffect(() => {
    store.getState().setRun(shownRun)
  }, [store, shownRun])

  // ── Save, estimate, validation ───────────────────────────────────────────
  const save = useAutosave(store, api, flowId, () => rf.getViewport(), {
    paused: offline || readOnly !== null,
    onRefused: markReadOnly,
  })
  useEstimate(store, api, !shownRun || isTerminal(shownRun.status))
  useRevalidate(store, api)

  useFlowChanged(flowId, (ev) => {
    if (ev.deleted) {
      toast.message(t('flows.editor.deletedElsewhere'))
      void navigate('/flows')
      return
    }
    save.noteRemoteRev(ev.rev)
  })

  // ── Graph ─────────────────────────────────────────────────────────────────
  const nodes = useStore(store, (s) => s.nodes)
  const edges = useStore(store, (s) => s.edges)
  const runNodes = useStore(store, (s) => s.run?.nodes)
  const displayEdges = useMemo<Edge[]>(() => {
    const ctx = { nodes, edges, schemas }
    return edges.map((e) => {
      const type = outputType(ctx, e.source, e.sourceHandle)
      const live = runNodes?.[e.target]?.state === 'running'
      return {
        ...e,
        animated: live,
        className: live ? 'flw-edge is-live' : 'flw-edge',
        style: { stroke: typeColor(type, nodesPayload) },
      }
    })
  }, [edges, nodes, schemas, runNodes, nodesPayload])

  const validConnection = useCallback(
    (conn: Edge | Connection) => {
      const s = store.getState()
      return isValidConnection(conn, { nodes: s.nodes, edges: s.edges, schemas })
    },
    [store, schemas],
  )

  const onConnectStart: OnConnectStart = useCallback(
    (_e, params) => {
      const s = store.getState()
      const ctx = { nodes: s.nodes, edges: s.edges, schemas }
      if (!params.nodeId || !params.handleId) {
        setDragType(null)
        return
      }
      if (params.handleType === 'source') {
        setDragType(outputType(ctx, params.nodeId, params.handleId))
      } else {
        const node = s.nodes.find((n) => n.id === params.nodeId)
        const spec = schemas
          .get(node?.data.type ?? '')
          ?.inputs.find((i) => i.name === params.handleId)
        setDragType(spec?.type ?? null)
      }
    },
    [store, schemas],
  )

  const onConnectEnd: OnConnectEnd = useCallback(
    (event, state) => {
      setDragType(null)
      if (state.isValid || state.toNode || !state.fromNode || !state.fromHandle?.id) return
      const point = 'changedTouches' in event ? event.changedTouches[0] : event
      if (!point) return
      const s = store.getState()
      const ctx = { nodes: s.nodes, edges: s.edges, schemas }
      const fromId = state.fromNode.id
      const handle = state.fromHandle.id
      const side = state.fromHandle.type
      let type: string | null = null
      if (side === 'source') type = outputType(ctx, fromId, handle)
      else {
        const node = s.nodes.find((n) => n.id === fromId)
        type =
          schemas.get(node?.data.type ?? '')?.inputs.find((i) => i.name === handle)?.type ?? null
      }
      if (!type) return
      readCanvasBox()
      setAddMenu({
        at: { x: point.clientX, y: point.clientY },
        flowAt: rf.screenToFlowPosition({ x: point.clientX, y: point.clientY }),
        filter: { side: side === 'source' ? 'input' : 'output', type },
        from: { nodeId: fromId, handle, side },
      })
    },
    [store, schemas, rf],
  )

  const addFromMenu = useCallback(
    (schema: NodeSchema, menu: AddMenuState) => {
      const s = store.getState()
      let link: Connection | null = null
      if (menu.from && menu.filter) {
        const handle = compatibleHandle(
          schema,
          menu.from.side === 'source' ? 'input' : 'output',
          menu.filter.type,
        )
        if (handle) {
          link =
            menu.from.side === 'source'
              ? {
                  source: menu.from.nodeId,
                  sourceHandle: menu.from.handle,
                  target: '',
                  targetHandle: handle,
                }
              : {
                  source: '',
                  sourceHandle: handle,
                  target: menu.from.nodeId,
                  targetHandle: menu.from.handle,
                }
          // Check it as if the node were already there.
          const probe: FlowCanvasNode = {
            id: '__new',
            type: 'flow',
            position: { x: 0, y: 0 },
            data: { type: schema.id, title: null, values: {} },
          }
          const ok = isValidConnection(
            { ...link, source: link.source || '__new', target: link.target || '__new' },
            { nodes: [...s.nodes, probe], edges: s.edges, schemas },
          )
          if (!ok) link = null
        }
      }
      // A node dragged off an input sits to the left of it, one off an output to the right.
      const at =
        menu.from?.side === 'target'
          ? { x: menu.flowAt.x - 250, y: menu.flowAt.y - 18 }
          : { x: menu.flowAt.x, y: menu.flowAt.y - 18 }
      store.getState().addNode(schema.id, at, link)
    },
    [store, schemas],
  )

  const openAddAt = useCallback(
    (clientX: number, clientY: number, flowAt?: { x: number; y: number }) => {
      if (!editable) return
      readCanvasBox()
      setAddMenu({
        at: { x: clientX, y: clientY },
        flowAt: flowAt ?? rf.screenToFlowPosition({ x: clientX, y: clientY }),
        filter: null,
        from: null,
      })
    },
    [rf, editable],
  )

  // ── Run / stop ────────────────────────────────────────────────────────────
  const [starting, setStarting] = useState(false)
  const run = useCallback(
    async (toNode?: string) => {
      // Never a silent no-op: every refusal says why.
      if (readOnly !== null) {
        toast.error(t('flows.readOnly.title'), {
          id: 'flows-read-only',
          description: readOnly || t('flows.readOnly.body'),
        })
        return
      }
      if (offline) {
        toast.error(t('flows.editor.offline'))
        return
      }
      if (starting) return
      setStarting(true)
      try {
        // The gateway runs what it holds: save first, so the run is the canvas.
        const saved = await save.flush()
        if (!saved.ok) {
          const code = flowsErrorCode(saved.error)
          // Refused writes and conflicts already told the person (banner / reload toast).
          if (code === FLOW_ERRORS.conflict) toast.message(t('flows.editor.runAfterConflict'))
          else if (code !== FLOW_ERRORS.operatorRequired) {
            toast.error(t('flows.editor.runNotSaved'), { description: errorText(saved.error) })
          }
          return
        }
        const res = await api.run({ flow_id: flowId, ...(toNode ? { to_node: toNode } : {}) })
        store.getState().setErrors({})
        setStarted({ runId: res.run_id, at: Date.now() })
      } catch (err) {
        const code = flowsErrorCode(err)
        if (code === FLOW_ERRORS.invalid) {
          store.getState().setErrors(nodeErrorsOf(err) ?? {})
          toast.error(t('flows.editor.invalid'))
        } else if (code === FLOW_ERRORS.busy) {
          toast.error(t('flows.editor.busy'))
        } else if (code === FLOW_ERRORS.costGate) {
          toast.error(t('flows.editor.costGate'), { description: errorText(err) })
        } else if (code === FLOW_ERRORS.unavailable) {
          toast.error(t('flows.editor.unavailable'), { description: errorText(err) })
        } else if (code === FLOW_ERRORS.operatorRequired) {
          markReadOnly(err)
        } else {
          toast.error(t('flows.editor.runFailed'), { description: errorText(err) })
        }
      } finally {
        setStarting(false)
      }
    },
    [readOnly, offline, starting, save, api, flowId, store, markReadOnly],
  )

  const stop = useCallback(async () => {
    if (!shownRunId || !running) return
    try {
      await api.stop(shownRunId)
    } catch (err) {
      if (flowsErrorCode(err) === FLOW_ERRORS.operatorRequired) markReadOnly(err)
      else toast.error(t('flows.editor.stopFailed'), { description: errorText(err) })
    }
  }, [api, shownRunId, running, markReadOnly])

  const selectedOne = () => {
    const picked = store.getState().nodes.filter((n) => n.selected)
    return picked.length === 1 ? picked[0]!.id : undefined
  }

  // ── Keyboard ──────────────────────────────────────────────────────────────
  const category = t('flows.shortcuts.category')
  useKeyboardShortcut(
    { combo: 'mod+enter', description: t('flows.shortcuts.run'), category, allowInInputs: true },
    (e) => {
      e.preventDefault()
      void run()
    },
  )
  useKeyboardShortcut(
    {
      combo: 'mod+shift+enter',
      description: t('flows.shortcuts.runTo'),
      category,
      allowInInputs: true,
    },
    (e) => {
      e.preventDefault()
      void run(selectedOne())
    },
  )
  useKeyboardShortcut(
    { combo: 'mod+.', description: t('flows.shortcuts.stop'), category, allowInInputs: true },
    (e) => {
      e.preventDefault()
      void stop()
    },
  )
  useKeyboardShortcut({ combo: 'mod+z', description: t('flows.shortcuts.undo'), category }, (e) => {
    e.preventDefault()
    if (editable) store.getState().undo()
  })
  useKeyboardShortcut(
    { combo: 'mod+shift+z', description: t('flows.shortcuts.redo'), category },
    (e) => {
      e.preventDefault()
      if (editable) store.getState().redo()
    },
  )
  useKeyboardShortcut(
    { combo: 'mod+d', description: t('flows.shortcuts.duplicate'), category },
    (e) => {
      e.preventDefault()
      if (editable) store.getState().duplicateSelected()
    },
  )
  useKeyboardShortcut(
    {
      combo: 'backspace',
      description: t('flows.shortcuts.delete'),
      category,
      documentationOnly: true,
    },
    () => {},
  )
  useKeyboardShortcut({ combo: 'mod+0', description: t('flows.shortcuts.fit'), category }, (e) => {
    e.preventDefault()
    void rf.fitView({ ...FIT_OPTIONS, duration: 200 })
  })
  useKeyboardShortcut({ combo: 'l', description: t('flows.shortcuts.lock'), category }, (e) => {
    if (!editable) return
    const s = store.getState()
    const picked = s.nodes.filter((n) => n.selected)
    let changed = false
    for (const n of picked) {
      const spec = schemas.get(n.data.type)?.inputs.find((i) => i.widget === 'variation')
      if (!spec) continue
      const current = n.data.values[spec.name] ?? spec.default ?? 'new'
      s.setValue(n.id, spec.name, current === 'keep' ? 'new' : 'keep')
      changed = true
    }
    if (changed) e.preventDefault()
  })

  // ── Image drop ────────────────────────────────────────────────────────────
  const addLoadImage = useCallback(
    (image: ImageRef, at: { x: number; y: number }) => {
      const type = [...schemas.values()].find((sc) =>
        sc.inputs.some((i) => i.widget === 'image_upload'),
      )
      const spec = type?.inputs.find((i) => i.widget === 'image_upload')
      if (!type || !spec) return
      store.getState().addNode(type.id, at, null, { [spec.name]: image })
    },
    [schemas, store],
  )

  const onDrop = useCallback(
    async (e: DragEvent<HTMLDivElement>) => {
      setDropping(false)
      const files = Array.from(e.dataTransfer.files ?? [])
      if (!files.length) return
      e.preventDefault()
      if (!editable) {
        toast.error(t('flows.readOnly.title'), {
          id: 'flows-read-only',
          description: readOnly || t('flows.readOnly.body'),
        })
        return
      }
      const base = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY })
      for (const [i, file] of files.entries()) {
        const at = { x: base.x + i * 30, y: base.y + i * 30 }
        const mime = droppedImageMime(file)
        if (!mime) {
          toast.error(t('flows.drop.unsupported'))
          continue
        }
        if (file.size > MAX_IMAGE_BYTES) {
          toast.error(t('flows.drop.tooBig'))
          continue
        }
        try {
          const data = await fileToBase64(file)
          if (mime === 'image/png') {
            const res = await api.importPng(data)
            if (res.flow && Array.isArray(res.flow.nodes) && res.flow.nodes.length) {
              setRestore({ image: res.image, flow: res.flow, at })
              continue
            }
            addLoadImage(res.image, at)
          } else {
            const res = await api.upload(file.name, mime, data)
            addLoadImage(res.image, at)
          }
        } catch (err) {
          toast.error(t('flows.drop.failed'), { description: errorText(err) })
        }
      }
    },
    [rf, api, addLoadImage, editable, readOnly],
  )

  const nodeTitle = useCallback(
    (id: string) => {
      const n = store.getState().nodes.find((x) => x.id === id)
      return n?.data.title || schemas.get(n?.data.type ?? '')?.display || id
    },
    [store, schemas],
  )

  // Live runs carry no graph: the canvas says which node is a Save, which a Load Image.
  const kinds = useMemo(
    () => ({
      typeOf: (nodeId: string) => store.getState().nodes.find((n) => n.id === nodeId)?.data.type,
      schemas,
    }),
    [store, schemas],
  )
  const tray = useMemo(() => trayRuns(summaries, tracker, kinds), [summaries, tracker, kinds])
  const empty = nodes.length === 0

  return (
    <EditorEnvContext.Provider value={env}>
      <div className="flw-editor">
        <EditorBar
          store={store}
          status={readOnly !== null ? 'readonly' : save.status}
          running={running}
          starting={starting}
          blocked={
            readOnly !== null
              ? t('flows.readOnly.title')
              : offline
                ? t('flows.editor.offline')
                : null
          }
          editable={editable}
          onRun={() => void run()}
          onStop={() => void stop()}
        />
        <div
          ref={canvasRef}
          className="flw-canvas"
          data-dropping={dropping || undefined}
          onDoubleClick={(e) => {
            const target = e.target as HTMLElement
            if (!target.closest('.react-flow__pane')) return
            openAddAt(e.clientX, e.clientY)
          }}
          onDragOver={(e) => {
            if (!Array.from(e.dataTransfer.types).includes('Files')) return
            e.preventDefault()
            e.dataTransfer.dropEffect = 'copy'
            if (!dropping) setDropping(true)
          }}
          onDragLeave={(e) => {
            if (e.currentTarget.contains(e.relatedTarget as Node | null)) return
            setDropping(false)
          }}
          onDrop={(e) => void onDrop(e)}
        >
          {readOnly !== null ? (
            <div className="flw-banner" role="alert">
              <ShieldAlert className="size-4 shrink-0" strokeWidth={1.75} aria-hidden />
              <div>
                <p className="flw-banner__title">{t('flows.readOnly.title')}</p>
                <p>{readOnly || t('flows.readOnly.body')}</p>
              </div>
            </div>
          ) : offline ? (
            <div className="flw-banner" role="status">
              <CloudOff className="size-4 shrink-0" strokeWidth={1.75} aria-hidden />
              <p className="flw-banner__title">{t('flows.editor.offline')}</p>
            </div>
          ) : null}
          <ReactFlow<FlowCanvasNode, Edge>
            nodes={nodes}
            edges={displayEdges}
            nodeTypes={NODE_TYPES}
            onNodesChange={store.getState().onNodesChange}
            onEdgesChange={store.getState().onEdgesChange}
            onConnect={store.getState().connect}
            onConnectStart={onConnectStart}
            onConnectEnd={onConnectEnd}
            isValidConnection={validConnection}
            defaultViewport={doc.viewport}
            fitView={!doc.viewport}
            fitViewOptions={FIT_OPTIONS}
            minZoom={0.2}
            maxZoom={2}
            zoomOnDoubleClick={false}
            panOnScroll
            deleteKeyCode={editable ? DELETE_KEYS : null}
            nodesDraggable={editable}
            nodesConnectable={editable}
            connectionLineStyle={{
              stroke: typeColor(dragType, nodesPayload),
              strokeWidth: 2.4,
              strokeDasharray: '3 5',
            }}
            proOptions={{ hideAttribution: false }}
          >
            <Background
              variant={BackgroundVariant.Dots}
              gap={22}
              size={1.2}
              color="var(--flw-dot)"
            />
          </ReactFlow>
          {/* Over a real graph it would sit on node headers; it is for getting started. */}
          {nodes.length < 2 ? (
            <div className="flw-hint" aria-hidden>
              {t('flows.editor.hint')} · <kbd className="kbd">⌘↵</kbd> {t('flows.editor.run')}
            </div>
          ) : null}
          {empty ? (
            <div className="flw-empty" aria-live="polite">
              <p className="flw-empty__title">{t('flows.editor.empty')}</p>
              <p>{t('flows.editor.empty.hint')}</p>
            </div>
          ) : null}
          {dropping ? <div className="flw-drop">{t('flows.drop.over')}</div> : null}
          <CanvasControls
            onAdd={(rect) => {
              const box = canvasRef.current?.getBoundingClientRect()
              const center = box
                ? rf.screenToFlowPosition({
                    x: box.left + box.width / 2,
                    y: box.top + box.height / 2,
                  })
                : undefined
              openAddAt(rect.right + 8, rect.top, center)
            }}
            store={store}
            canAdd={editable}
          />
        </div>
        <ResultsTray
          runs={tray}
          active={shownRun}
          activeStartedAt={started && started.runId === shownRunId ? started.at : null}
          kinds={kinds}
          nodeTitle={nodeTitle}
          onOpen={setViewer}
        />
      </div>

      {addMenu ? (
        <AddNodeMenu
          at={addMenu.at}
          bounds={canvasBox}
          nodes={nodesPayload}
          filter={addMenu.filter}
          onPick={(schema) => addFromMenu(schema, addMenu)}
          onClose={() => setAddMenu(null)}
        />
      ) : null}
      {picker ? (
        <ModelPicker
          nodeId={picker.nodeId}
          anchor={picker.anchor}
          bounds={canvasBox}
          onClose={() => setPicker(null)}
        />
      ) : null}
      {viewer ? <ImageViewer source={viewer} onClose={() => setViewer(null)} /> : null}
      {restore ? (
        <RestorePrompt
          onCancel={() => setRestore(null)}
          onRestore={() => {
            store.getState().replaceGraph(restore.flow)
            setRestore(null)
            requestAnimationFrame(() => void rf.fitView({ ...FIT_OPTIONS, duration: 200 }))
          }}
          onJustAdd={() => {
            addLoadImage(restore.image, restore.at)
            setRestore(null)
          }}
        />
      ) : null}
    </EditorEnvContext.Provider>
  )
}

// ── Toolbar ─────────────────────────────────────────────────────────────────

type SaveStatus = 'saved' | 'edited' | 'saving' | 'failed' | 'readonly'

function EditorBar({
  store,
  status,
  running,
  starting,
  blocked,
  editable,
  onRun,
  onStop,
}: {
  store: EditorStore
  status: SaveStatus
  running: boolean
  starting: boolean
  /** Why Run cannot work here (writes refused, gateway down), or null. */
  blocked: string | null
  /** False while the gateway refuses this window's writes. */
  editable: boolean
  onRun: () => void
  onStop: () => void
}) {
  const name = useStore(store, (s) => s.meta.name)
  const estimate = useStore(store, (s) => s.estimate)
  const canUndo = useStore(store, (s) => s.history.past.length > 0)
  const canRedo = useStore(store, (s) => s.history.future.length > 0)
  const chip = estimateChip(estimate)
  const statusText = {
    saved: t('flows.editor.saved'),
    edited: t('flows.editor.edited'),
    saving: t('flows.editor.saving'),
    failed: t('flows.editor.saveFailed'),
    readonly: t('flows.editor.readOnly'),
  }[status]
  return (
    <div className="flw-bar">
      <nav className="flw-crumb" aria-label={t('flows.editor.crumb')}>
        <Link to="/flows" className="flw-crumb__root app-no-drag">
          {t('flows.editor.crumb')}
        </Link>
        <span className="flw-crumb__sep" aria-hidden>
          /
        </span>
        <NameField
          name={name}
          readOnly={!editable}
          onRename={(next) => store.getState().setName(next)}
        />
        <span className="flw-crumb__status" data-status={status} aria-live="polite">
          · {statusText}
        </span>
      </nav>
      <span className="flex-1" />
      <span className="flw-bar__history">
        <button
          type="button"
          className="flw-icon-btn"
          aria-label={t('flows.editor.undo')}
          title={`${t('flows.editor.undo')} (⌘Z)`}
          disabled={!canUndo || !editable}
          onClick={() => store.getState().undo()}
        >
          <Undo2 className="size-3.5" strokeWidth={1.75} aria-hidden />
        </button>
        <button
          type="button"
          className="flw-icon-btn"
          aria-label={t('flows.editor.redo')}
          title={`${t('flows.editor.redo')} (⌘⇧Z)`}
          disabled={!canRedo || !editable}
          onClick={() => store.getState().redo()}
        >
          <Redo2 className="size-3.5" strokeWidth={1.75} aria-hidden />
        </button>
      </span>
      {chip ? (
        <span
          className="flw-chip"
          data-tone={chip.tone}
          title={estimate?.total.approximate ? t('flows.estimate.approx') : undefined}
        >
          <span>
            {chip.lead}
            {chip.tone === 'cost' ? ' ' : ' · '}
            <b className={chip.tone === 'cost' ? 'font-mono' : undefined}>{chip.amount}</b>
          </span>
          {chip.cached ? <span className="flw-chip__dim">· {chip.cached}</span> : null}
        </span>
      ) : null}
      <Button
        variant="ghost"
        disabled={!running}
        onClick={onStop}
        title={`${t('flows.editor.stop')} (⌘.)`}
      >
        {t('flows.editor.stop')} <kbd className="kbd">⌘.</kbd>
      </Button>
      <Button
        variant="primary"
        disabled={starting || blocked !== null}
        onClick={onRun}
        title={blocked ?? `${t('flows.editor.run')} (⌘↵)`}
      >
        <svg className="size-3.5" viewBox="0 0 24 24" aria-hidden>
          <path d="M7 5l11 7-11 7z" fill="currentColor" />
        </svg>
        {starting ? t('flows.editor.running') : t('flows.editor.run')}{' '}
        <kbd className="kbd flw-kbd-on-primary">⌘↵</kbd>
      </Button>
    </div>
  )
}

/** The flow's name in the breadcrumb: click to edit, Return or blur commits, Escape reverts. */
function NameField({
  name,
  readOnly,
  onRename,
}: {
  name: string
  readOnly: boolean
  onRename: (next: string) => void
}) {
  const [draft, setDraft] = useState(name)
  const [prev, setPrev] = useState(name)
  if (prev !== name) {
    setPrev(name)
    setDraft(name)
  }
  const ref = useRef<HTMLInputElement>(null)
  return (
    <input
      ref={ref}
      className="flw-crumb__name app-no-drag"
      value={draft}
      aria-label={t('flows.editor.rename')}
      maxLength={200}
      spellCheck={false}
      readOnly={readOnly}
      size={Math.max(8, Math.min(40, draft.length + 1))}
      onChange={(e) => setDraft(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') ref.current?.blur()
        if (e.key === 'Escape') {
          setDraft(name)
          requestAnimationFrame(() => ref.current?.blur())
        }
      }}
      onBlur={() => {
        if (!draft.trim()) setDraft(name)
        else if (draft.trim() !== name) onRename(draft)
      }}
    />
  )
}

function CanvasControls({
  onAdd,
  store,
  canAdd,
}: {
  onAdd: (rect: DOMRect) => void
  store: EditorStore
  canAdd: boolean
}) {
  const rf = useReactFlow()
  const { zoom } = useViewport()
  const addRef = useRef<HTMLButtonElement>(null)
  const count = useStore(store, (s) => s.nodes.length)
  return (
    <>
      <button
        ref={addRef}
        type="button"
        className="flw-add"
        aria-label={t('flows.editor.add')}
        title={t('flows.editor.add')}
        disabled={!canAdd}
        onClick={() => {
          const rect = addRef.current?.getBoundingClientRect()
          if (rect) onAdd(rect)
        }}
      >
        <Plus className="size-5" strokeWidth={1.75} aria-hidden />
      </button>
      <div className="flw-zoom" role="group">
        <button
          type="button"
          aria-label={t('flows.editor.zoomOut')}
          onClick={() => void rf.zoomOut({ duration: 120 })}
        >
          <Minus className="size-3" strokeWidth={2} aria-hidden />
        </button>
        <span className="font-mono">{Math.round(zoom * 100)}%</span>
        <button
          type="button"
          aria-label={t('flows.editor.zoomIn')}
          onClick={() => void rf.zoomIn({ duration: 120 })}
        >
          <Plus className="size-3" strokeWidth={2} aria-hidden />
        </button>
        <button
          type="button"
          aria-label={t('flows.editor.fit')}
          title={`${t('flows.editor.fit')} (⌘0)`}
          disabled={count === 0}
          onClick={() => void rf.fitView({ ...FIT_OPTIONS, duration: 200 })}
        >
          <Maximize className="size-3" strokeWidth={2} aria-hidden />
        </button>
      </div>
    </>
  )
}

function RestorePrompt({
  onRestore,
  onJustAdd,
  onCancel,
}: {
  onRestore: () => void
  onJustAdd: () => void
  onCancel: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={onCancel}
      overlayClassName="flw-alert__overlay"
      className="flw-alert"
    >
      <h2 id={titleId} className="flw-alert__title">
        {t('flows.drop.title')}
      </h2>
      <p id={bodyId} className="flw-alert__body">
        {t('flows.drop.body')}
      </p>
      <div className="flw-alert__actions">
        <Button onClick={onJustAdd}>{t('flows.drop.justAdd')}</Button>
        <Button variant="primary" onClick={onRestore}>
          {t('flows.drop.restore')}
        </Button>
      </div>
    </ModalShell>
  )
}

// ── Hooks ───────────────────────────────────────────────────────────────────

export type FlushResult = { ok: true } | { ok: false; error: unknown }

/**
 * Debounced `flows.save` with the revision the editor last saw. One save at
 * a time; edits made meanwhile go in the next one. A conflict (someone else
 * saved first) reloads the gateway's copy and says so; a `flows.changed`
 * from elsewhere reloads a clean editor quietly. A refusal of the write
 * itself (`flows.operator_required`) goes to `onRefused`; any other failure
 * says so once, with the gateway's words. While `paused` (gateway down, or
 * writes refused) nothing is sent; edits made meanwhile go out once it lifts.
 */
function useAutosave(
  store: EditorStore,
  api: FlowsApi,
  flowId: string,
  viewport: () => { x: number; y: number; zoom: number },
  opts: { paused: boolean; onRefused: (err: unknown) => void },
) {
  const queryClient = useQueryClient()
  const key = useStore(store, currentDocKey)
  const savedKey = useStore(store, (s) => s.savedKey)
  const [saving, setSaving] = useState(false)
  const [failed, setFailed] = useState(false)
  const inFlight = useRef<Promise<FlushResult> | null>(null)
  const remoteRev = useRef(0)
  const viewportRef = useRef(viewport)
  const refusedRef = useRef(opts.onRefused)
  useEffect(() => {
    viewportRef.current = viewport
    refusedRef.current = opts.onRefused
  }, [viewport, opts.onRefused])

  const reload = useCallback(
    async (why: 'conflict' | 'remote') => {
      try {
        const res = docWithAccess(await api.get(flowId))
        store.getState().loadDoc(res)
        if (!res.writable) refusedRef.current(new Error(res.read_only_reason ?? ''))
        if (why === 'conflict') toast.warning(t('flows.editor.conflict'))
        else toast.message(t('flows.editor.reloaded'))
      } catch (err) {
        toast.error(t('flows.offline.loadFailed'), { description: errorText(err) })
      }
    },
    [api, flowId, store],
  )

  /** One `flows.save` of whatever is on the canvas now. */
  const saveOnce = useCallback(async (): Promise<FlushResult> => {
    const s = store.getState()
    const sentKey = currentDocKey(s)
    const doc = graphToDoc(s.meta, s.history.present, viewportRef.current())
    try {
      const res = await api.save(doc, s.meta.rev)
      store.getState().markSaved(res.flow.rev, sentKey)
      setFailed(false)
      void queryClient.invalidateQueries({ queryKey: FLOWS_KEYS.list })
      return { ok: true }
    } catch (err) {
      const code = flowsErrorCode(err)
      if (code === FLOW_ERRORS.conflict) {
        await reload('conflict')
      } else if (code === FLOW_ERRORS.operatorRequired) {
        setFailed(true)
        refusedRef.current(err)
      } else {
        setFailed((was) => {
          if (!was) toast.error(t('flows.editor.saveFailedToast'), { description: errorText(err) })
          return true
        })
      }
      return { ok: false, error: err }
    }
  }, [api, store, reload, queryClient])

  /** Save now if anything is pending; resolves once the canvas as it is now is stored. */
  const flush = useCallback(async (): Promise<FlushResult> => {
    // One save on the wire at a time; edits made meanwhile go in the next one.
    while (inFlight.current) await inFlight.current
    if (!isDirty(store.getState())) return { ok: true }
    const attempt = saveOnce()
    inFlight.current = attempt
    setSaving(true)
    try {
      return await attempt
    } finally {
      inFlight.current = null
      setSaving(false)
      // Someone else's save was announced while ours was on the wire.
      if (remoteRev.current > store.getState().meta.rev && !isDirty(store.getState())) {
        remoteRev.current = 0
        void reload('remote')
      }
    }
  }, [store, saveOnce, reload])

  const paused = opts.paused
  useEffect(() => {
    if (paused || key === savedKey) return
    const timer = setTimeout(() => void flush(), 800)
    return () => clearTimeout(timer)
  }, [key, savedKey, flush, paused])

  // Leaving the editor saves what is pending.
  const flushRef = useRef(flush)
  const pausedRef = useRef(paused)
  useEffect(() => {
    flushRef.current = flush
    pausedRef.current = paused
  }, [flush, paused])
  useEffect(
    () => () => {
      if (!pausedRef.current) void flushRef.current()
    },
    [],
  )

  const noteRemoteRev = useCallback(
    (rev: number) => {
      if (rev <= store.getState().meta.rev) return
      if (inFlight.current) {
        remoteRev.current = Math.max(remoteRev.current, rev)
        return
      }
      // Dirty: the next save conflicts and reloads, with the message.
      if (!isDirty(store.getState())) void reload('remote')
    },
    [store, reload],
  )

  const status: SaveStatus = saving
    ? 'saving'
    : failed
      ? 'failed'
      : key === savedKey
        ? 'saved'
        : 'edited'
  return { status, flush, noteRemoteRev }
}

/** `flows.estimate` a moment after the graph settles, and again when a run settles. */
function useEstimate(store: EditorStore, api: FlowsApi, settled: boolean) {
  const graphKey = useStore(store, (s) => s.history.key)
  const seq = useRef(0)
  useEffect(() => {
    if (!settled) return
    const mine = ++seq.current
    const timer = setTimeout(async () => {
      const s = store.getState()
      try {
        const est = await api.estimate(graphToDoc(s.meta, s.history.present))
        if (mine === seq.current) store.getState().setEstimate(est)
      } catch {
        if (mine === seq.current) store.getState().setEstimate(null)
      }
    }, 600)
    return () => clearTimeout(timer)
  }, [graphKey, settled, api, store])
}

/** While validation errors are on screen, edits re-validate so fixed nodes clear. */
function useRevalidate(store: EditorStore, api: FlowsApi) {
  const graphKey = useStore(store, (s) => s.history.key)
  const showing = useStore(store, (s) => Object.keys(s.errors).length > 0)
  const seq = useRef(0)
  useEffect(() => {
    if (!showing) return
    const mine = ++seq.current
    const timer = setTimeout(async () => {
      const s = store.getState()
      try {
        const res = await api.validate(graphToDoc(s.meta, s.history.present))
        if (mine === seq.current) store.getState().setErrors(res.ok ? {} : (res.node_errors ?? {}))
      } catch {
        // Keep what is shown; the next run reports again.
      }
    }, 500)
    return () => clearTimeout(timer)
  }, [graphKey, showing, api, store])
}
