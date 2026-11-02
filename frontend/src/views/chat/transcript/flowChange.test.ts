import { beforeEach, describe, expect, it, vi } from 'vitest'

import { artifactCategory } from './artifacts'
import {
  FLOW_CHANGE_ARTIFACT_MIME,
  flowPriceText,
  normalizeFlowChangePayload,
  renderFlowChange,
} from './flowChange'
import { createFlowRunMounter } from './flowRun'
import { createLazyFlowRunMounter } from './flowRunLazy'
import { isFlowRunArtifact } from './flowRunMime'

const CHANGE = {
  version: 1,
  kind: 'flow_change',
  flow_id: 'flw_ab12cd34',
  name: 'AgentOS infographic',
  rev: 4,
  change: 'updated',
  summary: 'added writer (prompt_writer); changed gen (model)',
  node_count: 5,
  estimate: { kind: 'range', min: 0.0034, max: 0.1, approximate: true },
}

function host(): HTMLElement {
  const node = document.createElement('div')
  node.className = 'msg-artifact-flowrun'
  node.dataset.flowrunSrc = '/artifact/change.json'
  node.innerHTML = `<div class="msg-artifact-flowrun__body"></div>
    <p class="msg-artifact-flowrun__status">Loading Image Flow…</p>`
  document.body.append(node)
  return node
}

async function settle(): Promise<void> {
  for (let i = 0; i < 6; i += 1) await Promise.resolve()
  await new Promise((resolve) => setTimeout(resolve, 0))
}

beforeEach(() => {
  document.body.replaceChildren()
})

describe('flow change payload', () => {
  it('shares the flow-card placeholder (and its lazy chunk)', () => {
    expect(isFlowRunArtifact({ mime: FLOW_CHANGE_ARTIFACT_MIME })).toBe(true)
    expect(artifactCategory({ mime: FLOW_CHANGE_ARTIFACT_MIME, name: 'c.json' })).toBe('flowrun')
  })

  it('normalizes a change and refuses anything else', () => {
    expect(normalizeFlowChangePayload(CHANGE)).toEqual({
      flowId: 'flw_ab12cd34',
      name: 'AgentOS infographic',
      rev: 4,
      change: 'updated',
      summary: 'added writer (prompt_writer); changed gen (model)',
      nodeCount: 5,
      estimate: { kind: 'range', min: 0.0034, max: 0.1, usd: undefined, approximate: true },
    })
    expect(normalizeFlowChangePayload({ ...CHANGE, kind: 'flow_run' })).toBeNull()
    expect(normalizeFlowChangePayload({ ...CHANGE, flow_id: '' })).toBeNull()
    expect(normalizeFlowChangePayload({ ...CHANGE, change: 'created' })?.change).toBe('created')
  })

  it('writes prices like the editor', () => {
    expect(flowPriceText({ kind: 'free', approximate: false })).toBe('free')
    expect(flowPriceText({ kind: 'usd', usd: 0.0672, approximate: true })).toBe('~$0.067')
    expect(flowPriceText({ kind: 'range', min: 0.0034, max: 0.1, approximate: true })).toBe(
      '~$0.0034–$0.10',
    )
    expect(flowPriceText({ kind: 'unknown', approximate: true })).toBeNull()
    expect(flowPriceText(null)).toBeNull()
  })
})

describe('renderFlowChange', () => {
  it('draws name, change, summary, nodes, estimate per run and rev', () => {
    const node = host()
    const card = renderFlowChange(node, normalizeFlowChangePayload(CHANGE)!, { openFlow: null })

    expect(card.dataset.flowrunKind).toBe('change')
    expect(card.dataset.flowrunStatus).toBe('updated')
    expect(node.querySelector('.flowrun-card__title')?.textContent).toBe('AgentOS infographic')
    expect(node.querySelector('.flowrun-card__status')?.textContent).toBe('Updated')
    expect(node.querySelector('.flowrun-card__summary')?.textContent).toBe(CHANGE.summary)
    const facts = [...node.querySelectorAll('.flowrun-card__fact')].map((f) => f.textContent)
    expect(facts).toEqual(['5 nodes', '~$0.0034–$0.10 per run', 'rev 4'])
    // Read-only on the web: no Open in Flows without a handler.
    expect(node.querySelector('[data-flowrun-action="open"]')).toBeNull()
  })

  it('says free or unknown when that is what the estimate is', () => {
    const free = renderFlowChange(
      host(),
      normalizeFlowChangePayload({ ...CHANGE, estimate: { kind: 'free', approximate: false } })!,
      { openFlow: null },
    )
    expect(free.textContent).toContain('free to run')
    const unknown = renderFlowChange(
      host(),
      normalizeFlowChangePayload({ ...CHANGE, estimate: null })!,
      { openFlow: null },
    )
    expect(unknown.textContent).toContain('cost per run unknown')
  })

  it('writes agent text as text, never markup', () => {
    const node = host()
    renderFlowChange(
      node,
      normalizeFlowChangePayload({
        ...CHANGE,
        name: '<img src=x onerror=alert(1)>',
        summary: '<b>added</b>',
      })!,
      { openFlow: null },
    )
    expect(node.querySelector('.flowrun-card img, .flowrun-card b')).toBeNull()
    expect(node.querySelector('.flowrun-card__summary')?.textContent).toBe('<b>added</b>')
  })
})

describe('the flow-card mounter with a change card', () => {
  it('draws it without re-reading a run, and Open in Flows opens the flow', async () => {
    const call = vi.fn()
    const open = vi.fn()
    const mounter = createFlowRunMounter({
      fetchPayload: async () => ({ ...CHANGE, change: 'created' }),
      call,
      openFlow: () => open,
    })
    const node = host()

    mounter.mountFlowRuns(document.body)
    await settle()

    expect(call).not.toHaveBeenCalled()
    expect(node.querySelector('.flowrun-card__status')?.textContent).toBe('Created')
    expect(node.querySelector<HTMLElement>('.msg-artifact-flowrun__status')?.hidden).toBe(true)
    node.querySelector<HTMLButtonElement>('[data-flowrun-action="open"]')?.click()
    expect(open).toHaveBeenCalledWith('flw_ab12cd34')
  })

  it('loads the renderer lazily for a change placeholder too', async () => {
    const real = { mountFlowRuns: vi.fn(), runEvent: vi.fn(), reconnected: vi.fn() }
    const load = vi.fn(async () => ({ createFlowRunMounter: () => real }))
    const lazy = createLazyFlowRunMounter(
      { fetchPayload: async () => CHANGE },
      load as unknown as Parameters<typeof createLazyFlowRunMounter>[1],
    )
    const root = document.createElement('div')
    root.append(host())

    lazy.mountFlowRuns(root)

    await vi.waitFor(() => expect(real.mountFlowRuns).toHaveBeenCalledWith(root))
    expect(load).toHaveBeenCalledTimes(1)
  })
})
