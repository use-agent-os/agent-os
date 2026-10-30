import { describe, expect, it } from 'vitest'
import {
  effectiveValue,
  groupModels,
  hasSocket,
  isSocketOnly,
  isVisible,
  modelBlockReason,
  modelCaps,
  resolvedComboPatch,
  resolveOptions,
  schemaIndex,
  selectedModel,
  staleModelValues,
} from './schema-logic'
import { MODELS, NODES } from './test-fixtures'
import type { InputSpec } from './types'

const image = schemaIndex(NODES).get('image')!
const spec = (name: string): InputSpec => image.inputs.find((i) => i.name === name)!
const banana = MODELS.models[0]!
const gpt = MODELS.models[1]!

describe('sockets', () => {
  it('a typed input has a socket unless it says socket: false', () => {
    expect(hasSocket(spec('prompt'))).toBe(true)
    expect(hasSocket(spec('images'))).toBe(true)
    expect(hasSocket(spec('model'))).toBe(false)
    expect(hasSocket({ name: 'x', type: 'TEXT', widget: 'text', socket: false })).toBe(false)
    expect(isSocketOnly(spec('images'))).toBe(true)
    expect(isSocketOnly(spec('prompt'))).toBe(false)
  })
})

describe('selected model', () => {
  it('auto resolves to the registry default', () => {
    expect(selectedModel(image, { model: 'auto' }, MODELS)?.id).toBe(banana.id)
    expect(selectedModel(image, {}, MODELS)?.id).toBe(banana.id)
    expect(selectedModel(image, { model: gpt.id }, MODELS)?.id).toBe(gpt.id)
  })

  it('is null with no default, an unknown id, or a node without a model widget', () => {
    expect(selectedModel(image, { model: 'auto' }, { ...MODELS, default: null })).toBeNull()
    expect(selectedModel(image, { model: 'nope/x' }, MODELS)).toBeNull()
    expect(selectedModel(schemaIndex(NODES).get('text'), {}, MODELS)).toBeNull()
  })
})

describe('show_when', () => {
  it('shows a widget only when the selected model has a non-empty cap', () => {
    expect(isVisible(spec('aspect'), banana)).toBe(true)
    expect(isVisible(spec('resolution'), banana)).toBe(true)
    expect(isVisible(spec('size'), banana)).toBe(false)
    expect(isVisible(spec('quality'), banana)).toBe(false)
    expect(isVisible(spec('background'), banana)).toBe(false)

    expect(isVisible(spec('aspect'), gpt)).toBe(false)
    expect(isVisible(spec('size'), gpt)).toBe(true)
    expect(isVisible(spec('quality'), gpt)).toBe(true)
    expect(isVisible(spec('background'), gpt)).toBe(true)
  })

  it('hides model-dependent widgets when there is no model, keeps the rest', () => {
    expect(isVisible(spec('aspect'), null)).toBe(false)
    expect(isVisible(spec('count'), null)).toBe(true)
    expect(isVisible(spec('prompt'), null)).toBe(true)
  })
})

describe('options_from', () => {
  it('reads the choices from the selected model', () => {
    expect(resolveOptions(spec('aspect'), banana)).toEqual(['1:1', '3:2', '2:3', '16:9'])
    expect(resolveOptions(spec('resolution'), banana)).toEqual(['1K', '2K', '4K'])
    expect(resolveOptions(spec('size'), gpt)).toEqual([
      'auto',
      '1024x1024',
      '1536x1024',
      '1024x1536',
    ])
    expect(resolveOptions(spec('aspect'), null)).toEqual([])
  })

  it('falls back to the schema list without options_from', () => {
    expect(resolveOptions(spec('background'), gpt)).toEqual(['auto', 'opaque', 'transparent'])
  })

  it('shows the stored value when offered, else the default, else the first option', () => {
    expect(effectiveValue(spec('aspect'), { aspect: '3:2' }, banana)).toBe('3:2')
    expect(effectiveValue(spec('aspect'), { aspect: '9:21' }, banana)).toBe('1:1')
    expect(effectiveValue(spec('resolution'), {}, banana)).toBe('1K')
    expect(effectiveValue(spec('count'), {}, banana)).toBe(1)
    expect(effectiveValue(spec('variation'), {}, banana)).toBe('new')
    expect(effectiveValue(spec('model'), {}, banana)).toBe('auto')
  })

  it('names the combos showing a value they do not store, with what they show', () => {
    const flux = { ...gpt, output: { sizes: ['1024x1024', '1024x768'] } }
    const withAuto = {
      ...image,
      inputs: image.inputs.map((i) => (i.name === 'size' ? { ...i, default: 'auto' } : i)),
    }
    expect(resolvedComboPatch(withAuto, {}, flux)).toEqual({ size: '1024x1024' })
    expect(resolvedComboPatch(withAuto, { size: '1024x768' }, flux)).toEqual({})
    // A stale choice, or a model list with no default, resolves to what is shown;
    // hidden combos and schema lists with their default in place need nothing.
    expect(resolvedComboPatch(image, { aspect: '9:21' }, banana)).toEqual({
      aspect: '1:1',
      resolution: '1K',
    })
    expect(resolvedComboPatch(image, { size: '1024x1024', quality: 'high' }, gpt)).toEqual({})
  })

  it('lists the values a model change leaves stale', () => {
    const values = { aspect: '3:2', resolution: '2K', count: 2 }
    expect(staleModelValues(image, values, gpt)).toEqual(['aspect', 'resolution'])
    expect(staleModelValues(image, values, banana)).toEqual([])
  })
})

describe('model picker facts', () => {
  it('says why a model cannot take the job', () => {
    expect(modelBlockReason(gpt, 0)).toBe('OPENAI_API_KEY is not set')
    expect(modelBlockReason(banana, 0)).toBeNull()
    expect(modelBlockReason(banana, 14)).toBeNull()
    expect(modelBlockReason(banana, 15)).toBe('Takes up to 14 images')
    expect(modelBlockReason({ ...banana, ops: { t2i: true, edit: false } }, 1)).toBe(
      'Cannot edit images',
    )
  })

  it('groups by provider in registry order and hides deprecated models unless selected', () => {
    const old = { ...banana, id: 'openrouter/old', status: 'deprecated' }
    const groups = groupModels([banana, old, gpt], null)
    expect(groups.map((g) => [g.provider, g.available, g.models.length])).toEqual([
      ['openrouter', true, 1],
      ['openai', false, 1],
    ])
    expect(groupModels([banana, old, gpt], 'openrouter/old')[0]!.models).toHaveLength(2)
  })

  it('summarises caps', () => {
    expect(modelCaps(banana)).toBe('t2i · edit · 14 refs · 1K–4K · 4 ratios')
    expect(modelCaps(gpt)).toBe('t2i · edit · 16 refs · mask · n≤8 · 4 sizes · transparent bg')
  })
})
