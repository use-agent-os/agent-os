import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { artifactCategory } from './artifacts'
import {
  FLOW_RUN_ARTIFACT_MIME,
  FLOW_RUN_THUMB_SIDE,
  cardFromRun,
  createFlowRunMounter,
  formatFlowUsd,
  isFlowRunArtifact,
  normalizeFlowRunPayload,
  thumbnailUrl,
} from './flowRun'

const SHA_A = 'a'.repeat(64)
const SHA_B = 'b'.repeat(64)

const SNAPSHOT = {
  version: 1,
  kind: 'flow_run',
  run_id: 'run_0123456789ab',
  flow_id: 'flw_ab12cd34',
  flow_name: 'Lighthouse study',
  status: 'running',
  progress: { done: 1, total: 3 },
  cached: 1,
  est_usd: 0.0672,
  cost_usd: null,
  images: [],
  error: null,
}

function runRecord(status: string) {
  const done = status === 'done'
  return {
    run_id: 'run_0123456789ab',
    flow_id: 'flw_ab12cd34',
    status,
    graph: {
      nodes: [
        { id: 'n1', type: 'text' },
        { id: 'n2', type: 'image' },
        { id: 'n3', type: 'save' },
      ],
    },
    nodes: {
      n1: { state: 'cached' },
      n2: { state: done ? 'done' : 'running' },
      n3: { state: done ? 'done' : 'queued' },
    },
    outputs: done
      ? {
          n2: { images: [{ sha256: SHA_A, width: 1024, height: 1024 }] },
          n3: {
            images: [{ sha256: SHA_B, mime: 'image/png', width: 1024, height: 1024 }],
            files: ['/state/flows/outputs/flw_ab12cd34/lighthouse_run_1.png'],
          },
        }
      : {},
    est_usd: 0.0672,
    cost_usd: done ? 0.05 : null,
    error: null,
  }
}

function host(): HTMLElement {
  const node = document.createElement('div')
  node.className = 'msg-artifact-flowrun'
  node.dataset.flowrunSrc = '/artifact/run.json'
  node.innerHTML = `<div class="msg-artifact-flowrun__body"></div>
    <p class="msg-artifact-flowrun__status">Loading flow run…</p>`
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

afterEach(() => {
  vi.useRealTimers()
})

describe('pure helpers', () => {
  it('matches the flow-run mime and gives it its own artifact category', () => {
    expect(isFlowRunArtifact({ mime: FLOW_RUN_ARTIFACT_MIME })).toBe(true)
    expect(isFlowRunArtifact({ mime: `${FLOW_RUN_ARTIFACT_MIME}; charset=utf-8` })).toBe(true)
    expect(isFlowRunArtifact({ mime: 'application/json' })).toBe(false)
    expect(isFlowRunArtifact(null)).toBe(false)
    expect(artifactCategory({ mime: FLOW_RUN_ARTIFACT_MIME, name: 'flow-run-x.json' })).toBe(
      'flowrun',
    )
  })

  it('normalizes a snapshot and refuses anything else', () => {
    const card = normalizeFlowRunPayload(SNAPSHOT)
    expect(card).toMatchObject({
      runId: 'run_0123456789ab',
      flowName: 'Lighthouse study',
      status: 'running',
      progress: { done: 1, total: 3 },
      cached: 1,
      estUsd: 0.0672,
      costUsd: null,
    })
    expect(normalizeFlowRunPayload({ ...SNAPSHOT, kind: 'mandate' })).toBeNull()
    expect(normalizeFlowRunPayload({ ...SNAPSHOT, run_id: '' })).toBeNull()
    expect(normalizeFlowRunPayload({ ...SNAPSHOT, status: 'weird' })?.status).toBe('unknown')
  })

  it('derives the card from a run record: saved images with file names win', () => {
    const card = cardFromRun(runRecord('done'), 'Lighthouse study')
    expect(card?.status).toBe('done')
    expect(card?.progress).toEqual({ done: 3, total: 3 })
    expect(card?.cached).toBe(1)
    expect(card?.costUsd).toBe(0.05)
    expect(card?.images.map((i) => [i.sha256, i.name])).toEqual([[SHA_B, 'lighthouse_run_1.png']])

    const noSave = runRecord('done')
    delete (noSave.outputs as Record<string, unknown>).n3
    expect(cardFromRun(noSave)?.images.map((i) => [i.sha256, i.name])).toEqual([[SHA_A, null]])
  })

  it('formats money and builds only image data URLs', () => {
    expect(formatFlowUsd(0.05)).toBe('$0.050')
    expect(formatFlowUsd(0.5)).toBe('$0.50')
    expect(formatFlowUsd(null)).toBe('—')
    expect(thumbnailUrl({ mime: 'image/png', data_b64: 'iVBORw0KGgo=' })).toBe(
      'data:image/png;base64,iVBORw0KGgo=',
    )
    expect(thumbnailUrl({ mime: 'text/html', data_b64: 'PHNjcmlwdD4=' })).toBe('')
    expect(thumbnailUrl({ mime: 'image/png', data_b64: '"><script>' })).toBe('')
  })
})

describe('createFlowRunMounter', () => {
  it('draws the snapshot, re-reads the run live and fetches 256px thumbnails', async () => {
    const call = vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method === 'flows.run.get') return { run: runRecord('done') }
      if (method === 'flows.media') return { mime: 'image/png', data_b64: 'iVBORw0KGgo=', params }
      throw new Error(method)
    })
    const mounter = createFlowRunMounter({ fetchPayload: async () => SNAPSHOT, call })
    const node = host()

    mounter.mountFlowRuns(document.body)
    await settle()

    expect(call).toHaveBeenCalledWith('flows.run.get', { run_id: 'run_0123456789ab' })
    expect(call).toHaveBeenCalledWith('flows.media', {
      sha256: SHA_B,
      max_side: FLOW_RUN_THUMB_SIDE,
    })
    const card = node.querySelector<HTMLElement>('.flowrun-card')
    expect(card?.dataset.flowrunStatus).toBe('done')
    expect(node.querySelector('.flowrun-card__title')?.textContent).toBe('Lighthouse study')
    expect(node.querySelector('.flowrun-card__progress')?.textContent).toBe('3/3 nodes')
    expect(node.querySelector('.flowrun-card__meta')?.textContent).toContain('1 cached')
    expect(node.querySelector('.flowrun-card__meta')?.textContent).toContain('cost $0.050')
    const img = node.querySelector<HTMLImageElement>('.flowrun-thumb img')
    expect(img?.src).toBe('data:image/png;base64,iVBORw0KGgo=')
    expect(node.querySelector('.flowrun-thumb__name')?.textContent).toBe('lighthouse_run_1.png')
    expect(node.querySelector<HTMLElement>('.msg-artifact-flowrun__status')?.hidden).toBe(true)
    // Read-only on the web: no Open in Flows without a handler.
    expect(node.querySelector('[data-flowrun-action="open"]')).toBeNull()
  })

  it('without a gateway call shows the snapshot with file names only', async () => {
    const mounter = createFlowRunMounter({
      fetchPayload: async () => ({
        ...SNAPSHOT,
        status: 'done',
        images: [{ sha256: SHA_B, name: 'fox.png' }],
      }),
    })
    const node = host()

    mounter.mountFlowRuns(document.body)
    await settle()

    expect(node.querySelector('.flowrun-thumb img')).toBeNull()
    expect(node.querySelector('.flowrun-thumb__img--empty')).not.toBeNull()
    expect(node.querySelector('.flowrun-thumb__name')?.textContent).toBe('fox.png')
  })

  it('offers Open in Flows when the caller hands over a handler', async () => {
    const open = vi.fn()
    const mounter = createFlowRunMounter({
      fetchPayload: async () => SNAPSHOT,
      openFlow: () => open,
    })
    const node = host()

    mounter.mountFlowRuns(document.body)
    await settle()
    node.querySelector<HTMLButtonElement>('[data-flowrun-action="open"]')?.click()

    expect(open).toHaveBeenCalledWith('flw_ab12cd34')
  })

  it('a re-mounted card draws the newest known state, not its stale snapshot', async () => {
    let answer = runRecord('done')
    const call = vi.fn(async (method: string) => {
      if (method === 'flows.run.get') return { run: answer }
      return { mime: 'image/png', data_b64: 'AAAA' }
    })
    const mounter = createFlowRunMounter({ fetchPayload: async () => SNAPSHOT, call })
    host()
    mounter.mountFlowRuns(document.body)
    await settle()

    // The transcript rebuilds the row; the live read now fails.
    document.body.replaceChildren()
    answer = undefined as never
    call.mockImplementation(async () => {
      throw new Error('offline')
    })
    const again = host()
    mounter.mountFlowRuns(document.body)
    await settle()

    expect(again.querySelector<HTMLElement>('.flowrun-card')?.dataset.flowrunStatus).toBe('done')
  })

  it('folds run events into one re-read and refreshes at once on the end', async () => {
    vi.useFakeTimers()
    const call = vi.fn<(method: string, params?: unknown) => Promise<unknown>>(async () => ({
      run: runRecord('running'),
    }))
    const mounter = createFlowRunMounter({ fetchPayload: async () => SNAPSHOT, call })
    host()
    mounter.mountFlowRuns(document.body)
    await vi.runAllTimersAsync()
    call.mockClear()

    mounter.runEvent({ run_id: 'run_0123456789ab', node_id: 'n2', state: 'running' })
    mounter.runEvent({ run_id: 'run_0123456789ab', node_id: 'n2', state: 'done' })
    mounter.runEvent({ run_id: 'run_other', status: 'done' })
    await vi.advanceTimersByTimeAsync(500)
    const reads = () => call.mock.calls.filter((args) => args[0] === 'flows.run.get').length
    expect(reads()).toBe(1)

    call.mockResolvedValue({ run: runRecord('done') })
    mounter.runEvent({ run_id: 'run_0123456789ab', status: 'done' })
    await vi.advanceTimersByTimeAsync(0)
    expect(reads()).toBe(2)
  })

  it('writes user text as text, never markup', async () => {
    const mounter = createFlowRunMounter({
      fetchPayload: async () => ({
        ...SNAPSHOT,
        flow_name: '<img src=x onerror=alert(1)>',
        status: 'failed',
        error: { code: 'x', message: '<b>boom</b>' },
      }),
    })
    const node = host()

    mounter.mountFlowRuns(document.body)
    await settle()

    expect(node.querySelector('.flowrun-card__title')?.textContent).toBe(
      '<img src=x onerror=alert(1)>',
    )
    expect(node.querySelector('.flowrun-card img')).toBeNull()
    expect(node.querySelector('.flowrun-card__error')?.textContent).toBe('<b>boom</b>')
    expect(node.querySelector('.flowrun-card b')).toBeNull()
  })

  it('reports an unreadable payload in the placeholder', async () => {
    const mounter = createFlowRunMounter({ fetchPayload: async () => ({ kind: 'other' }) })
    const node = host()

    mounter.mountFlowRuns(document.body)
    await settle()

    expect(node.querySelector('.msg-artifact-flowrun__status')?.textContent).toBe(
      'Flow run data could not be read.',
    )
  })
})
