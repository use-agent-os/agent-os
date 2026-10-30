import { t } from '~/i18n'
import type {
  CapKey,
  InputSpec,
  ModelOutputCaps,
  ModelsPayload,
  ModelWire,
  NodeSchema,
  NodesPayload,
} from './types'

/**
 * Pure reading of the node schema (contract §3) against the model registry
 * (§4): which inputs carry a socket, which widgets the selected model shows,
 * where a combo's options come from, and what value a widget displays.
 */

/** A widget input with a `type` also has a socket unless it says `socket: false`. */
export function hasSocket(spec: InputSpec): boolean {
  if (!spec.type) return false
  return spec.socket ?? true
}

/** Socket only: there is nothing to type into, the value can only arrive on a link. */
export function isSocketOnly(spec: InputSpec): boolean {
  return hasSocket(spec) && !spec.widget
}

export function schemaIndex(payload: NodesPayload | undefined): Map<string, NodeSchema> {
  return new Map((payload?.nodes ?? []).map((n) => [n.id, n]))
}

export function inputSpec(schema: NodeSchema | undefined, name: string): InputSpec | undefined {
  return schema?.inputs.find((i) => i.name === name)
}

export function outputSpec(schema: NodeSchema | undefined, name: string) {
  return schema?.outputs.find((o) => o.name === name)
}

/** The node's model widget, if it has one (the `image` node does). */
export function modelInput(schema: NodeSchema | undefined): InputSpec | undefined {
  return schema?.inputs.find((i) => i.widget === 'model')
}

/**
 * The model a node runs on: its `model` value, or the registry default when
 * that value is `auto` (or missing). Null when there is no usable answer.
 */
export function selectedModel(
  schema: NodeSchema | undefined,
  values: Record<string, unknown>,
  models: ModelsPayload | undefined,
): ModelWire | null {
  const spec = modelInput(schema)
  if (!spec || !models) return null
  const raw = values[spec.name] ?? spec.default ?? 'auto'
  const id = raw === 'auto' || raw == null || raw === '' ? models.default : String(raw)
  if (!id) return null
  return models.models.find((m) => m.id === id) ?? null
}

/** `show_when: {"model.has": X}`: the selected model's caps have a non-empty X. */
export function modelHas(model: ModelWire | null, cap: CapKey): boolean {
  if (!model) return false
  const value = (model.output as ModelOutputCaps)[cap]
  if (Array.isArray(value)) return value.length > 0
  return value === true
}

export function isVisible(spec: InputSpec, model: ModelWire | null): boolean {
  const cap = spec.show_when?.['model.has']
  if (!cap) return true
  return modelHas(model, cap)
}

const OPTIONS_FROM: Record<string, keyof ModelOutputCaps> = {
  'model.ratios': 'ratios',
  'model.tiers': 'tiers',
  'model.sizes': 'sizes',
  'model.qualities': 'qualities',
}

/** A combo's choices: from the selected model's caps, or the schema's own list. */
export function resolveOptions(spec: InputSpec, model: ModelWire | null): string[] {
  if (spec.options_from) {
    const key = OPTIONS_FROM[spec.options_from]
    const list = key && model ? model.output[key] : undefined
    return Array.isArray(list) ? list.map(String) : []
  }
  return spec.options ?? []
}

/**
 * What a widget shows. A combo whose stored value is not one of the current
 * options (the model changed underneath it) shows the schema default when that
 * is an option, else the first option — the value the engine would use.
 */
export function effectiveValue(
  spec: InputSpec,
  values: Record<string, unknown>,
  model: ModelWire | null,
): unknown {
  const stored = values[spec.name]
  if (spec.widget === 'combo') {
    const options = resolveOptions(spec, model)
    if (options.length === 0) return stored ?? spec.default ?? ''
    if (typeof stored === 'string' && options.includes(stored)) return stored
    if (typeof spec.default === 'string' && options.includes(spec.default)) return spec.default
    return options[0]
  }
  if (stored !== undefined) return stored
  if (spec.default !== undefined) return spec.default
  switch (spec.widget) {
    case 'number':
      return spec.min ?? 0
    case 'toggle':
      return false
    case 'model':
      return 'auto'
    case 'variation':
      return 'new'
    case 'image_upload':
      return null
    default:
      return ''
  }
}

/**
 * Values to drop after the model changes: combos fed by the model whose stored
 * choice the new model does not offer, so they fall back to its default
 * instead of failing validation.
 */
export function staleModelValues(
  schema: NodeSchema,
  values: Record<string, unknown>,
  model: ModelWire | null,
): string[] {
  const stale: string[] = []
  for (const spec of schema.inputs) {
    if (!spec.options_from || values[spec.name] === undefined) continue
    const options = resolveOptions(spec, model)
    if (!options.includes(String(values[spec.name]))) stale.push(spec.name)
  }
  return stale
}

/**
 * Combos (visible under `model`) whose stored or default value is not one of
 * the model's options: the value each one shows instead (its first option,
 * which is what the engine runs with). Written into the node on its next
 * edit, so what is saved is what was on screen. FLUX.2 Pro's sizes have no
 * "auto", for one.
 */
export function resolvedComboPatch(
  schema: NodeSchema,
  values: Record<string, unknown>,
  model: ModelWire | null,
): Record<string, unknown> {
  const patch: Record<string, unknown> = {}
  for (const spec of schema.inputs) {
    if (spec.widget !== 'combo' || !isVisible(spec, model)) continue
    const options = resolveOptions(spec, model)
    if (!options.length) continue
    const stored = values[spec.name] ?? spec.default
    if (typeof stored === 'string' && options.includes(stored)) continue
    if (stored === undefined && !spec.options_from) continue
    patch[spec.name] = effectiveValue(spec, values, model)
  }
  return patch
}

/** Defaults written into a brand-new node: nothing, the schema fills the gaps. */
export function initialValues(): Record<string, unknown> {
  return {}
}

export function clampNumber(spec: InputSpec, value: number): number {
  let next = Number.isFinite(value) ? value : (spec.min ?? 0)
  if (spec.min !== undefined) next = Math.max(spec.min, next)
  if (spec.max !== undefined) next = Math.min(spec.max, next)
  return next
}

/** Short capability line for the model picker: "t2i · edit · 14 refs · 1K–4K · 15 ratios". */
export function modelCaps(model: ModelWire): string {
  const parts: string[] = []
  if (model.ops.t2i) parts.push('t2i')
  if (model.ops.edit) parts.push('edit')
  if (model.max_images) parts.push(`${model.max_images} ${t('flows.caps.refs')}`)
  if (model.ops.mask) parts.push('mask')
  const n = model.output.n_max ?? 1
  if (n > 1) parts.push(`n≤${n}`)
  const tiers = model.output.tiers ?? []
  if (tiers.length > 1) parts.push(`${tiers[0]}–${tiers[tiers.length - 1]}`)
  else if (tiers.length === 1) parts.push(tiers[0]!)
  const ratios = model.output.ratios ?? []
  if (ratios.length) parts.push(`${ratios.length} ${t('flows.caps.ratios')}`)
  const sizes = model.output.sizes ?? []
  if (sizes.length) parts.push(`${sizes.length} ${t('flows.caps.sizes')}`)
  if (model.output.transparent_bg) parts.push(t('flows.caps.transparent'))
  return parts.join(' · ')
}

/**
 * Why a model cannot take this node's job, or null when it can: no key, or
 * the images connected need an edit-capable model with room for all of them.
 */
export function modelBlockReason(model: ModelWire, imageCount: number): string | null {
  if (!model.available) return model.unavailable_reason || t('flows.model.unavailable')
  if (imageCount > 0 && model.max_images === 0) return t('flows.model.t2iOnly')
  if (imageCount > 0 && !model.ops.edit) return t('flows.model.noEdit')
  if (model.max_images !== undefined && imageCount > model.max_images) {
    return t('flows.model.maxImages').replace('{n}', String(model.max_images))
  }
  if (imageCount === 0 && model.ops.t2i === false) return t('flows.model.needsImage')
  return null
}

export interface ProviderGroup {
  provider: string
  available: boolean
  models: ModelWire[]
}

/** Models grouped by provider, in registry order; deprecated ones only while selected. */
export function groupModels(models: ModelWire[], selectedId: string | null): ProviderGroup[] {
  const groups = new Map<string, ProviderGroup>()
  for (const model of models) {
    if (model.status === 'deprecated' && model.id !== selectedId) continue
    let group = groups.get(model.provider)
    if (!group) {
      group = { provider: model.provider, available: false, models: [] }
      groups.set(model.provider, group)
    }
    group.models.push(model)
    if (model.available) group.available = true
  }
  return [...groups.values()]
}

const PROVIDER_NAMES: Record<string, string> = {
  openrouter: 'OpenRouter',
  openai: 'OpenAI',
  google: 'Google',
  gemini: 'Gemini',
}

export function providerName(id: string): string {
  return PROVIDER_NAMES[id] ?? id.charAt(0).toUpperCase() + id.slice(1)
}
