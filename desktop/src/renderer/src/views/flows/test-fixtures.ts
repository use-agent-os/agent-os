import type { FlowDoc, ImageRef, ModelsPayload, NodesPayload } from './types'

/**
 * Contract v1 payloads (plans/image-flows/contract.md §2–§4) for the unit
 * tests. Imported by `*.test.ts` only; nothing in the app reads this file.
 */

export const NODES: NodesPayload = {
  version: 1,
  types: { TEXT: { color: '#c4c4cc' }, IMAGE: { color: '#60a5fa' } },
  nodes: [
    {
      id: 'text',
      display: 'Text',
      group: 'input',
      inputs: [{ name: 'text', widget: 'textarea', socket: false, default: '' }],
      outputs: [{ name: 'text', type: 'TEXT' }],
      price: null,
    },
    {
      id: 'load_image',
      display: 'Load Image',
      group: 'input',
      inputs: [{ name: 'image', widget: 'image_upload', socket: false, default: null }],
      outputs: [{ name: 'image', type: 'IMAGE' }],
      price: null,
    },
    {
      id: 'join_text',
      display: 'Join Text',
      group: 'utility',
      inputs: [
        { name: 'a', type: 'TEXT', widget: 'textarea' },
        { name: 'b', type: 'TEXT', widget: 'textarea', optional: true },
        { name: 'template', widget: 'text', socket: false, default: '{a}, {b}' },
      ],
      outputs: [{ name: 'text', type: 'TEXT' }],
      price: null,
    },
    {
      id: 'prompt_writer',
      display: 'Prompt Writer',
      group: 'ai',
      is_api: true,
      inputs: [
        { name: 'idea', type: 'TEXT', widget: 'textarea' },
        { name: 'context', type: 'TEXT', optional: true },
        {
          name: 'style',
          widget: 'combo',
          socket: false,
          options: ['photographic', 'illustration', '3d render', 'anime', 'minimal', 'none'],
          default: 'photographic',
        },
      ],
      outputs: [{ name: 'text', type: 'TEXT' }],
      price: { depends_on: [] },
    },
    {
      id: 'describe_image',
      display: 'Describe Image',
      group: 'ai',
      is_api: true,
      inputs: [
        { name: 'image', type: 'IMAGE' },
        {
          name: 'focus',
          widget: 'combo',
          socket: false,
          options: ['everything', 'composition', 'style', 'subject', 'colors'],
          default: 'everything',
        },
      ],
      outputs: [{ name: 'text', type: 'TEXT' }],
      price: { depends_on: [] },
    },
    {
      id: 'image',
      display: 'Image',
      group: 'ai',
      is_api: true,
      inputs: [
        { name: 'prompt', type: 'TEXT', widget: 'textarea', default: '' },
        { name: 'images', type: 'IMAGE', optional: true, multi: true },
        { name: 'model', widget: 'model', socket: false, default: 'auto' },
        {
          name: 'aspect',
          widget: 'combo',
          socket: false,
          options_from: 'model.ratios',
          show_when: { 'model.has': 'ratios' },
          default: '1:1',
        },
        {
          name: 'resolution',
          widget: 'combo',
          socket: false,
          options_from: 'model.tiers',
          show_when: { 'model.has': 'tiers' },
        },
        {
          name: 'size',
          widget: 'combo',
          socket: false,
          options_from: 'model.sizes',
          show_when: { 'model.has': 'sizes' },
        },
        {
          name: 'quality',
          widget: 'combo',
          socket: false,
          options_from: 'model.qualities',
          show_when: { 'model.has': 'qualities' },
        },
        {
          name: 'background',
          widget: 'combo',
          socket: false,
          options: ['auto', 'opaque', 'transparent'],
          show_when: { 'model.has': 'transparent_bg' },
          default: 'auto',
        },
        { name: 'count', widget: 'number', socket: false, default: 1, min: 1, max: 4, step: 1 },
        { name: 'variation', widget: 'variation', socket: false, default: 'new' },
      ],
      outputs: [{ name: 'image', type: 'IMAGE' }],
      price: { depends_on: ['model', 'resolution', 'size', 'quality', 'count', 'images.count'] },
    },
    {
      id: 'save',
      display: 'Save',
      group: 'output',
      is_output: true,
      inputs: [
        { name: 'images', type: 'IMAGE', multi: true },
        { name: 'name', widget: 'text', socket: false, default: '{flow}_{run}_{n}' },
      ],
      outputs: [],
      price: null,
    },
  ],
}

export const MODELS: ModelsPayload = {
  default: 'openrouter/nano-banana-2',
  models: [
    {
      id: 'openrouter/nano-banana-2',
      display: 'Nano Banana 2',
      provider: 'openrouter',
      family: 'gemini-image',
      status: 'active',
      available: true,
      ops: { t2i: true, edit: true, mask: false },
      max_images: 14,
      output: {
        ratios: ['1:1', '3:2', '2:3', '16:9'],
        tiers: ['1K', '2K', '4K'],
        sizes: [],
        qualities: [],
        transparent_bg: false,
        n_max: 1,
      },
      seed_honored: false,
      price: { per_image: { '1K': 0.039, '2K': 0.12 }, per_ref: 0, approximate: true },
    },
    {
      id: 'openai/gpt-image-1',
      display: 'GPT Image',
      provider: 'openai',
      status: 'active',
      available: false,
      unavailable_reason: 'OPENAI_API_KEY is not set',
      ops: { t2i: true, edit: true, mask: true },
      max_images: 16,
      output: {
        ratios: [],
        tiers: [],
        sizes: ['auto', '1024x1024', '1536x1024', '1024x1536'],
        qualities: ['auto', 'low', 'medium', 'high'],
        transparent_bg: true,
        n_max: 8,
      },
      price: { per_image: { 'medium:1024x1024': 0.042 }, approximate: true },
    },
  ],
}

export const IMG: ImageRef = {
  sha256: 'a'.repeat(64),
  mime: 'image/png',
  width: 1024,
  height: 1024,
  bytes: 1234,
}

export const DOC: FlowDoc = {
  version: 1,
  id: 'flw_ab12cd34',
  name: 'Lighthouse study',
  rev: 17,
  updated: '2026-10-29T10:00:00Z',
  nodes: [
    { id: 'n3', type: 'text', pos: [20, 300], title: null, values: { text: 'a lighthouse' } },
    {
      id: 'n4',
      type: 'prompt_writer',
      pos: [300, 250],
      title: null,
      values: { style: 'photographic' },
    },
    {
      id: 'n5',
      type: 'image',
      pos: [880, 20],
      title: null,
      values: { prompt: '', model: 'auto', aspect: '3:2', count: 4, variation: 'new' },
    },
    { id: 'n6', type: 'load_image', pos: [20, 20], title: 'Reference', values: { image: IMG } },
    { id: 'n7', type: 'save', pos: [1220, 20], title: null, values: {} },
  ],
  links: [
    { id: 'l1', from: ['n3', 'text'], to: ['n4', 'idea'] },
    { id: 'l2', from: ['n4', 'text'], to: ['n5', 'prompt'] },
    { id: 'l3', from: ['n6', 'image'], to: ['n5', 'images'] },
    { id: 'l4', from: ['n5', 'image'], to: ['n7', 'images'] },
  ],
  inputs: { idea: 'n3.text' },
  viewport: { x: 0, y: 0, zoom: 0.82 },
}
