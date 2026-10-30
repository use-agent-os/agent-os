import { Handle, Position, type NodeProps } from '@xyflow/react'
import { Check, CircleAlert, TriangleAlert } from 'lucide-react'
import { memo, useState, type CSSProperties } from 'react'
import { useStore } from 'zustand'
import { useShallow } from 'zustand/react/shallow'
import { t } from '~/i18n'
import { cn } from '~/lib/utils'
import { useNow } from '~/lib/use-now'
import { useFlowThumb } from '~/stores/flows'
import { typeColor, useEditorEnv } from './editor-context'
import type { FlowCanvasNode } from './graph'
import { NodeIcon } from './icons'
import { badgeText, formatElapsed, formatMs, usd } from './price'
import {
  effectiveValue,
  hasSocket,
  isSocketOnly,
  isVisible,
  resolveOptions,
  resolvedComboPatch,
  selectedModel,
} from './schema-logic'
import { isSettledNode, type ViewNodeKind, type ViewNodeState } from './run-tracker'
import type { ImageRef, InputSpec, NodeError, NodeSchema } from './types'
import { LinkedWidget, WIDGETS, widgetLabel } from './widgets'

/**
 * Every node on the canvas, drawn from its schema: a header (icon, title,
 * price badge, state), the socket-only inputs and the outputs, one widget
 * row per visible input, then what the last run produced. Sockets are
 * coloured by link type; a socket is hollow until linked when the input can
 * go without a link (optional, or a widget that holds its own value).
 */

interface Source {
  title: string
  text: string | null
}

function sockStyle(color: string): CSSProperties {
  return { ['--c' as string]: color }
}

export const FlowNode = memo(function FlowNode({ id, data, selected }: NodeProps<FlowCanvasNode>) {
  const env = useEditorEnv()
  const schema = env.schemas.get(data.type)
  const model = selectedModel(schema, data.values, env.models)
  const defaultModel = env.models?.models.find((m) => m.id === env.models?.default) ?? null

  // Flat string maps, so the shallow compare sees equal values as equal.
  const sourceTitles = useStore(
    env.store,
    useShallow((s) => {
      const out: Record<string, string> = {}
      for (const e of s.edges) {
        if (e.target !== id || !e.targetHandle || out[e.targetHandle]) continue
        const node = s.nodes.find((n) => n.id === e.source)
        // Several links into a multi input: the first names it.
        out[e.targetHandle] =
          node?.data.title || env.schemas.get(node?.data.type ?? '')?.display || e.source
      }
      return out
    }),
  )
  const sourceTexts = useStore(
    env.store,
    useShallow((s) => {
      const out: Record<string, string> = {}
      for (const e of s.edges) {
        if (e.target !== id || !e.targetHandle || e.targetHandle in out) continue
        const text = s.run?.outputs[e.source]?.text
        if (typeof text === 'string') out[e.targetHandle] = text
      }
      return out
    }),
  )
  const linkCounts = useStore(
    env.store,
    useShallow((s) => {
      const out: Record<string, number> = {}
      for (const e of s.edges) {
        if (e.target === id && e.targetHandle) out[e.targetHandle] = (out[e.targetHandle] ?? 0) + 1
      }
      return out
    }),
  )
  const usedOutputs = useStore(
    env.store,
    useShallow((s) => s.edges.filter((e) => e.source === id).map((e) => e.sourceHandle ?? '')),
  )
  const state = useStore(env.store, (s) => s.run?.nodes[id])
  const startedAt = useStore(env.store, (s) => s.run?.nodeStartedAt[id])
  const outputs = useStore(env.store, (s) => s.run?.outputs[id])
  const price = useStore(env.store, (s) => s.estimate?.nodes[id])
  const cached = useStore(env.store, (s) => s.estimate?.cached.includes(id) ?? false)
  const errors = useStore(env.store, (s) => s.errors[id])

  if (!schema) return <UnknownNode type={data.type} selected={selected} />

  const title = data.title || schema.display
  const validation = errors ?? []
  const runError = state?.state === 'error' ? state.error : undefined
  const inputErrors = new Map<string, NodeError>()
  for (const err of [...validation, ...(runError ? [runError] : [])]) {
    if (err.input && !inputErrors.has(err.input)) inputErrors.set(err.input, err)
  }
  const nodeErrors = validation.filter((e) => !e.input)
  const invalid = validation.length > 0
  const failed = Boolean(runError)

  const portInputs = schema.inputs.filter((i) => isSocketOnly(i))
  // A combo fed by the model with nothing to offer (FLUX.2 Pro has neither
  // sizes nor ratios) is left out rather than drawn blank.
  const widgetInputs = schema.inputs.filter(
    (i) =>
      i.widget &&
      isVisible(i, model) &&
      !(i.widget === 'combo' && i.options_from && resolveOptions(i, model).length === 0),
  )
  const multiImage = schema.inputs.find((i) => i.multi && i.type === 'IMAGE')
  const imageCount = multiImage ? (linkCounts[multiImage.name] ?? 0) : 0
  const showMode = Boolean(multiImage && schema.inputs.some((i) => i.widget === 'model'))
  const expected = Number(
    data.values.count ?? schema.inputs.find((i) => i.name === 'count')?.default ?? 1,
  )

  const nodeState: ViewNodeKind | 'invalid' | undefined = invalid ? 'invalid' : state?.state
  // Combos showing a value they do not store (the model's first option): written on the next edit.
  const comboFixes = resolvedComboPatch(schema, data.values, model)
  const badge = badgeText(price)

  return (
    <div
      className={cn('flw-node', `flw-node--${schema.id}`)}
      data-state={nodeState}
      data-selected={selected || undefined}
    >
      <div className="flw-node__head">
        <span className="flw-node__icon" aria-hidden>
          <NodeIcon id={schema.id} group={schema.group} className="size-[11px]" strokeWidth={2} />
        </span>
        <NodeTitle nodeId={id} title={title} custom={Boolean(data.title)} />
        <span className="flw-node__state">
          {/* The estimate until the node has run; then its real cost is in the state tag. */}
          {badge && !(cached && !state) && (!state || !isSettledNode(state.state)) ? (
            <span
              className="flw-badge"
              title={price?.approximate ? t('flows.estimate.approx') : undefined}
            >
              {badge}
            </span>
          ) : null}
          <StateTag
            state={state}
            startedAt={startedAt}
            invalid={invalid}
            cachedPlan={cached && !state}
          />
        </span>
      </div>
      {state?.state === 'running' ? (
        <div className="flw-node__prog" aria-hidden>
          <i
            style={{
              width: state.progress?.max
                ? `${Math.min(100, (100 * state.progress.value) / state.progress.max)}%`
                : undefined,
            }}
            data-indeterminate={!state.progress?.max || undefined}
          />
        </div>
      ) : null}
      {showMode ? (
        <div className="flw-node__mode" data-edit={imageCount > 0 || undefined}>
          <i aria-hidden />
          {imageCount === 0
            ? t('flows.node.mode.t2i')
            : t(imageCount === 1 ? 'flows.node.mode.edit' : 'flows.node.mode.editMany').replace(
                '{n}',
                String(imageCount),
              )}
        </div>
      ) : null}

      {portInputs.length || schema.outputs.length ? (
        <div className="flw-node__io">
          <div className="flw-node__ins">
            {portInputs.map((spec) => (
              <PortIn
                key={spec.name}
                spec={spec}
                links={linkCounts[spec.name] ?? 0}
                max={spec.multi && spec.type === 'IMAGE' ? model?.max_images : undefined}
                invalid={inputErrors.has(spec.name)}
                color={typeColor(spec.type, env.nodes)}
              />
            ))}
          </div>
          <div className="flw-node__outs">
            {schema.outputs.map((out) => (
              <div key={out.name} className="flw-port flw-port--out">
                <span>{out.type}</span>
                <Handle
                  type="source"
                  position={Position.Right}
                  id={out.name}
                  className="flw-sock"
                  data-filled={usedOutputs.includes(out.name) || undefined}
                  style={sockStyle(typeColor(out.type, env.nodes))}
                  aria-label={`${out.name} (${out.type})`}
                />
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {widgetInputs.length ? (
        <div className="flw-node__widgets" inert={env.readOnly || undefined}>
          {widgetInputs.map((spec) => (
            <WidgetRow
              key={spec.name}
              nodeId={id}
              spec={spec}
              values={data.values}
              model={model}
              defaultModel={defaultModel}
              source={
                sourceTitles[spec.name]
                  ? { title: sourceTitles[spec.name]!, text: sourceTexts[spec.name] ?? null }
                  : undefined
              }
              linked={(linkCounts[spec.name] ?? 0) > 0}
              error={inputErrors.get(spec.name)}
              color={typeColor(spec.type, env.nodes)}
              fixes={comboFixes}
            />
          ))}
        </div>
      ) : null}

      {outputs?.text ? (
        <div className="flw-out nowheel" data-selectable>
          <b>{t('flows.node.output')}</b>
          {outputs.text}
        </div>
      ) : null}
      {outputs?.images?.length || (state?.state === 'running' && multiImage) ? (
        <ImageGrid
          images={outputs?.images ?? []}
          pending={
            state?.state === 'running' ? Math.max(0, expected - (outputs?.images?.length ?? 0)) : 0
          }
        />
      ) : null}
      {outputs?.note ? (
        <p className="flw-note" data-selectable>
          <TriangleAlert className="size-3 shrink-0" strokeWidth={2} aria-hidden />
          {outputs.note}
        </p>
      ) : null}

      {nodeErrors.length || (failed && !runError?.input) ? (
        <div className="flw-err" role="alert">
          {nodeErrors.map((e, i) => (
            <p key={i}>{e.message}</p>
          ))}
          {failed && runError && !runError.input ? <p>{runError.message}</p> : null}
          {invalid ? <span className="flw-err__trace">{t('flows.node.validate')}</span> : null}
        </div>
      ) : null}
    </div>
  )
})

function NodeTitle({ nodeId, title, custom }: { nodeId: string; title: string; custom: boolean }) {
  const env = useEditorEnv()
  const [editing, setEditing] = useState(false)
  if (editing) {
    return (
      <input
        className="flw-node__title-input nodrag"
        defaultValue={custom ? title : ''}
        placeholder={title}
        aria-label={t('flows.node.rename')}
        autoFocus
        onBlur={(e) => {
          setEditing(false)
          env.store.getState().setTitle(nodeId, e.target.value)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
          if (e.key === 'Escape') setEditing(false)
        }}
      />
    )
  }
  return (
    <span
      className="flw-node__title"
      title={t('flows.node.rename')}
      onDoubleClick={(e) => {
        e.stopPropagation()
        if (!env.readOnly) setEditing(true)
      }}
    >
      {title}
    </span>
  )
}

/** "2/4 · 0:47" or "running · 0:47": a long provider call still shows it is alive. */
function RunningTag({ state, startedAt }: { state: ViewNodeState; startedAt: number | undefined }) {
  const now = useNow(1000)
  const head = state.progress?.max
    ? `${state.progress.value}/${state.progress.max}`
    : t('flows.node.running')
  return (
    <span className="flw-tag flw-tag--live">
      {startedAt !== undefined ? `${head} · ${formatElapsed(now - startedAt)}` : head}
    </span>
  )
}

function StateTag({
  state,
  startedAt,
  invalid,
  cachedPlan,
}: {
  state: ViewNodeState | undefined
  startedAt: number | undefined
  invalid: boolean
  cachedPlan: boolean
}) {
  if (invalid) return <span className="flw-tag flw-tag--danger">{t('flows.node.invalid')}</span>
  if (!state) {
    return cachedPlan ? <span className="flw-tag">{t('flows.node.cached')}</span> : null
  }
  switch (state.state) {
    case 'queued':
      return <span className="flw-tag flw-tag--warn">{t('flows.node.queued')}</span>
    case 'running':
      return <RunningTag state={state} startedAt={startedAt} />
    case 'cached':
      return <span className="flw-tag">{t('flows.node.cached')}</span>
    case 'stopping':
      // Stopped while its call was in flight: the call finishes (and is paid for).
      return (
        <span className="flw-tag flw-tag--stopping">
          <i className="flw-tag__dot" aria-hidden />
          {t('flows.node.stopping')}
        </span>
      )
    case 'skipped':
      // A call stopped mid-flight may still report what it cost.
      return (
        <span className="flw-tag">
          {state.cost_usd
            ? `${t('flows.node.skipped')} · ${usd(state.cost_usd)}`
            : t('flows.node.skipped')}
        </span>
      )
    case 'error':
      return (
        <span className="flw-tag flw-tag--danger">
          <CircleAlert className="size-3" strokeWidth={2} aria-hidden />
          {t('flows.node.error')}
        </span>
      )
    case 'done': {
      if (state.stopped) {
        return (
          <span className="flw-tag">
            {state.cost_usd
              ? `${t('flows.node.stopped')} · ${usd(state.cost_usd)}`
              : t('flows.node.stopped')}
          </span>
        )
      }
      const bits = [
        formatMs(state.ms),
        state.cost_usd !== undefined && state.cost_usd > 0 ? usd(state.cost_usd) : null,
      ].filter(Boolean)
      return (
        <span className="flw-tag">
          <Check className="size-3 text-ok" strokeWidth={2.5} aria-hidden />
          {bits.join(' · ')}
        </span>
      )
    }
  }
}

function PortIn({
  spec,
  links,
  max,
  invalid,
  color,
}: {
  spec: InputSpec
  links: number
  max?: number
  invalid: boolean
  color: string
}) {
  // A text-to-image-only model takes no images: the socket stays (an existing
  // link must still show, flagged) but takes no new link.
  const closed = max === 0
  const hollow = links === 0 && (spec.optional === true || closed)
  let note: string | null = null
  if (closed && links === 0) note = t('flows.node.t2iOnly')
  else if (spec.multi && max !== undefined) note = `${links} / ${max}`
  else if (spec.optional) note = t('flows.node.optional')
  return (
    <div
      className={cn(
        'flw-port flw-port--in',
        invalid && 'is-invalid',
        closed && links === 0 && 'is-disabled',
      )}
    >
      <Handle
        type="target"
        position={Position.Left}
        id={spec.name}
        isConnectable={!closed}
        className="flw-sock"
        data-filled={!hollow || undefined}
        data-hollow={hollow || undefined}
        style={sockStyle(color)}
        aria-label={`${spec.name} (${spec.type})`}
      />
      <span>{widgetLabel(spec)}</span>
      {note ? (
        <span
          className="flw-port__note"
          data-over={max !== undefined && links > max ? true : undefined}
        >
          {note}
        </span>
      ) : null}
    </div>
  )
}

function WidgetRow({
  nodeId,
  spec,
  values,
  model,
  defaultModel,
  source,
  linked,
  error,
  color,
  fixes,
}: {
  nodeId: string
  spec: InputSpec
  values: Record<string, unknown>
  model: ReturnType<typeof selectedModel>
  defaultModel: ReturnType<typeof selectedModel>
  source: Source | undefined
  linked: boolean
  error: NodeError | undefined
  color: string
  fixes: Record<string, unknown>
}) {
  const env = useEditorEnv()
  const socket = hasSocket(spec)
  const Widget = spec.widget ? WIDGETS[spec.widget] : null
  return (
    <div className="flw-wrow" data-socket={socket || undefined}>
      {socket ? (
        <Handle
          type="target"
          position={Position.Left}
          id={spec.name}
          className="flw-sock"
          data-filled={linked || undefined}
          data-hollow={!linked || undefined}
          style={sockStyle(color)}
          aria-label={`${spec.name} (${spec.type})`}
        />
      ) : null}
      {linked && source ? (
        <LinkedWidget
          spec={spec}
          source={source.title}
          text={source.text}
          invalid={Boolean(error)}
        />
      ) : Widget ? (
        <Widget
          nodeId={nodeId}
          spec={spec}
          value={effectiveValue(spec, values, model)}
          model={model}
          defaultModel={defaultModel}
          invalid={Boolean(error)}
          onChange={(value, coalesce) => {
            const editor = env.store.getState()
            const others = Object.keys(fixes).filter((k) => k !== spec.name)
            if (!others.length) editor.setValue(nodeId, spec.name, value, coalesce)
            else editor.patchValues(nodeId, { ...fixes, [spec.name]: value })
          }}
        />
      ) : null}
      {error ? <p className="flw-wrow__err">{error.message}</p> : null}
    </div>
  )
}

function ImageGrid({ images, pending }: { images: ImageRef[]; pending: number }) {
  const env = useEditorEnv()
  const cells = images.length + pending
  return (
    <div className="flw-grid" data-single={cells === 1 || undefined}>
      {images.map((img, i) => (
        <GridThumb
          key={`${img.sha256}:${i}`}
          image={img}
          onOpen={() => env.openImages(images, i)}
        />
      ))}
      {Array.from({ length: pending }, (_, i) => (
        <div key={`wait-${i}`} className="flw-grid__wait">
          {t('flows.node.waiting')}
        </div>
      ))}
    </div>
  )
}

function GridThumb({ image, onOpen }: { image: ImageRef; onOpen: () => void }) {
  const thumb = useFlowThumb(image)
  return (
    <button
      type="button"
      className="flw-grid__cell nodrag"
      aria-label={t('flows.node.openImage')}
      onClick={onOpen}
    >
      {thumb.src ? <img src={thumb.src} alt="" draggable={false} /> : null}
    </button>
  )
}

function UnknownNode({ type, selected }: { type: string; selected: boolean }) {
  const schema: Pick<NodeSchema, 'display'> = { display: type }
  return (
    <div className="flw-node" data-state="error" data-selected={selected || undefined}>
      <div className="flw-node__head">
        <span className="flw-node__icon" aria-hidden>
          <CircleAlert className="size-[11px]" strokeWidth={2} />
        </span>
        <span className="flw-node__title">{schema.display}</span>
      </div>
      <div className="flw-err">
        <p>
          <b>{t('flows.node.unknownType')}</b>
        </p>
        <p>{t('flows.node.unknownType.body')}</p>
      </div>
    </div>
  )
}
