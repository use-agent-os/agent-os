import { readFileSync } from 'node:fs'
import { beforeEach, describe, expect, it } from 'vitest'
import { normalizeFlowRunPayload, renderFlowRun } from '@/views/chat/transcript/flowRun'

// The desktop skins the shared Image Flow run card (frontend flowRun.ts) in
// its own chat.css. Render the REAL card with every branch on and hold the
// skin to the classes it emits, so the DOM and the stylesheet cannot drift.

const css = readFileSync('src/renderer/src/views/chat/chat.css', 'utf8')

function card(status: string) {
  const payload = normalizeFlowRunPayload({
    kind: 'flow_run',
    run_id: 'run_0123456789ab',
    flow_id: 'flw_ab12cd34',
    flow_name: 'Lighthouse study',
    status,
    progress: { done: 2, total: 3 },
    cached: 1,
    cost_usd: 0.05,
    images: Array.from({ length: 9 }, (_, i) => ({
      sha256: String(i)
        .repeat(64)
        .slice(0, 64)
        .replace(/[^0-9a-f]/g, 'a'),
      name: `lighthouse_${i}.png`,
    })),
    error: status === 'failed' ? { code: 'x', message: 'The provider refused the prompt' } : null,
  })!
  const host = document.createElement('div')
  host.innerHTML = '<div class="msg-artifact-flowrun__body"></div>'
  document.body.append(host)
  return renderFlowRun(host, payload, {
    thumb: (sha) => (sha.startsWith('0') ? 'data:image/png;base64,AAAA' : ''),
    openFlow: () => {},
  })
}

beforeEach(() => {
  document.body.replaceChildren()
})

describe('desktop Image Flow run card skin', () => {
  it('has a rule for every class the shared renderer emits', () => {
    const root = card('failed')
    const classes = new Set<string>()
    ;[root, ...root.querySelectorAll<HTMLElement>('*')].forEach((node) =>
      node.classList.forEach((name) => classes.add(name)),
    )
    expect(classes.size).toBeGreaterThan(10)
    for (const name of classes) {
      expect(css, `.${name} has no desktop rule`).toContain(`.${name}`)
    }
  })

  it('tones the rail from the renderer-stamped status', () => {
    expect(card('running').dataset.flowrunStatus).toBe('running')
    for (const status of ['running', 'done', 'failed']) {
      expect(css).toContain(`.flowrun-card[data-flowrun-status='${status}']`)
    }
    expect(css).toMatch(/\.flowrun-card::before\s*{[^}]*background: var\(--flowrun-rail\)/)
  })

  it('draws the placeholder group like the other inline cards', () => {
    expect(css).toMatch(/\.msg-artifact-flowrun-group\s*{\s*display: grid;/)
    expect(css).toContain('.msg-artifact-flowrun__status[hidden]')
  })
})
