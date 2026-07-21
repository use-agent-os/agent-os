import { describe, expect, it, vi } from 'vitest'
import {
  modelDisplayName,
  routerFxStripProvider,
  routerFxNormalizeRequestKind,
  routerFxRequestKindFromAttachments,
  routerFxSeedCacheKey,
  routerFxSortTiers,
  routerFxNormalizeTier,
  routerFxIdentity,
  routerFxDecisionIdentity,
  routerFxUsageIdentity,
  createRouterFxRegistry,
  routerFxVisualEntries,
  routerFxHasMultipleCandidates,
  createRouterFxRenderer,
  type RouterFxRegistry,
} from './routerFx'

/* ── modelDisplayName / routerFxStripProvider (parity chat.js:3444/3451) ── */

describe('modelDisplayName (parity chat.js:3444)', () => {
  it('strips a provider prefix at the last "/"', () => {
    expect(modelDisplayName('anthropic/claude-x')).toBe('claude-x')
    expect(modelDisplayName('z-ai/glm-5.1')).toBe('glm-5.1')
  })
  it('strips a trailing 8-digit date suffix', () => {
    expect(modelDisplayName('glm-5.1-20260406')).toBe('glm-5.1')
  })
  it('strips both prefix and date suffix', () => {
    expect(modelDisplayName('vendor/glm-5.1-20260406')).toBe('glm-5.1')
  })
  it('leaves a bare name untouched', () => {
    expect(modelDisplayName('claude-x')).toBe('claude-x')
  })
  it('returns non-string / falsy input unchanged', () => {
    expect(modelDisplayName('')).toBe('')
    // @ts-expect-error legacy tolerates non-string input and returns it as-is
    expect(modelDisplayName(null)).toBe(null)
    // @ts-expect-error legacy tolerates non-string input and returns it as-is
    expect(modelDisplayName(undefined)).toBe(undefined)
  })
})

describe('routerFxStripProvider (parity chat.js:3451)', () => {
  it('delegates to modelDisplayName', () => {
    expect(routerFxStripProvider('anthropic/claude-x')).toBe('claude-x')
    expect(routerFxStripProvider('glm-5.1-20260406')).toBe('glm-5.1')
  })
})

/* ── routerFxNormalizeRequestKind (parity chat.js:3464) ── */

describe('routerFxNormalizeRequestKind (parity chat.js:3464)', () => {
  it('maps only "image" to image, everything else to text', () => {
    expect(routerFxNormalizeRequestKind('image')).toBe('image')
    expect(routerFxNormalizeRequestKind('text')).toBe('text')
    // case-sensitive: "TEXT" and "text" both normalize to text (neither is "image")
    expect(routerFxNormalizeRequestKind('TEXT')).toBe(routerFxNormalizeRequestKind('text'))
    // "IMAGE" is NOT lowercased by this helper, so it is not "image" → text
    expect(routerFxNormalizeRequestKind('IMAGE')).toBe('text')
    expect(routerFxNormalizeRequestKind(undefined)).toBe('text')
  })
})

/* ── routerFxRequestKindFromAttachments (parity chat.js:3455) ── */

describe('routerFxRequestKindFromAttachments (parity chat.js:3455)', () => {
  it('returns image when any attachment has an image/* mime', () => {
    expect(routerFxRequestKindFromAttachments([{ mime: 'image/png' }])).toBe('image')
  })
  it('reads the "type" field when "mime" is absent', () => {
    expect(routerFxRequestKindFromAttachments([{ type: 'IMAGE/JPEG' }])).toBe('image')
  })
  it('returns text for non-image attachments', () => {
    expect(routerFxRequestKindFromAttachments([{ mime: 'text/plain' }])).toBe('text')
  })
  it('returns text for an empty or non-array input', () => {
    expect(routerFxRequestKindFromAttachments([])).toBe('text')
    // @ts-expect-error legacy tolerates non-array input
    expect(routerFxRequestKindFromAttachments(null)).toBe('text')
  })
})

/* ── routerFxSeedCacheKey (parity chat.js:3582) ── */

describe('routerFxSeedCacheKey (parity chat.js:3582)', () => {
  it('assembles the exact prefixed key with turnIndex coerced via | 0', () => {
    expect(routerFxSeedCacheKey('agent:main', 3, 'c1')).toBe('osq.routerFx.seed:agent:main:3:c1')
  })
  it('coerces a nullish session key to "" and a nullish turnIndex to 0', () => {
    // @ts-expect-error legacy coerces null sessionKey to ""
    expect(routerFxSeedCacheKey(null, null, 'layout')).toBe('osq.routerFx.seed::0:layout')
  })
})

/* ── routerFxNormalizeTier / routerFxSortTiers (parity chat.js:3430/3419) ── */

describe('routerFxNormalizeTier (parity chat.js:3430)', () => {
  it('lowercases and rewrites t0-t3 to c0-c3', () => {
    expect(routerFxNormalizeTier('T2')).toBe('c2')
    expect(routerFxNormalizeTier('t0')).toBe('c0')
    expect(routerFxNormalizeTier('C1')).toBe('c1')
  })
  it('leaves an unrecognized tier lowercased but otherwise intact', () => {
    expect(routerFxNormalizeTier('image_model')).toBe('image_model')
    // t4 is out of the 0-3 range, so it is only lowercased
    expect(routerFxNormalizeTier('T4')).toBe('t4')
  })
  it('returns "" for empty / nullish input', () => {
    expect(routerFxNormalizeTier('')).toBe('')
    expect(routerFxNormalizeTier(null)).toBe('')
  })
})

describe('routerFxSortTiers (parity chat.js:3419)', () => {
  it('orders c<n> numerically, ahead of non-c tiers which sort lexically', () => {
    expect(routerFxSortTiers(['c10', 'c2', 'zeta', 'c1', 'alpha'])).toEqual([
      'c1',
      'c2',
      'c10',
      'alpha',
      'zeta',
    ])
  })
  it('does not mutate the input array', () => {
    const input = ['c2', 'c1']
    routerFxSortTiers(input)
    expect(input).toEqual(['c2', 'c1'])
  })
})

/* ── identity helpers (parity chat.js:3631-3643) ── */

describe('routerFxIdentity (parity chat.js:3631)', () => {
  it('joins lowercased trimmed model and normalized tier with "|"', () => {
    expect(routerFxIdentity('  Anthropic/Claude  ', 'T1')).toBe('anthropic/claude|c1')
  })
  it('returns "" when both parts are empty', () => {
    expect(routerFxIdentity('', '')).toBe('')
  })
  it('keeps the separator when only one side is present', () => {
    expect(routerFxIdentity('gpt', '')).toBe('gpt|')
    expect(routerFxIdentity('', 't2')).toBe('|c2')
  })
})

describe('routerFxDecisionIdentity / routerFxUsageIdentity (parity chat.js:3637/3641)', () => {
  it('reads decision model/tier with routed_* fallbacks', () => {
    expect(routerFxDecisionIdentity({ model: 'M', tier: 't1' })).toBe('m|c1')
    expect(routerFxDecisionIdentity({ routed_model: 'M2', routed_tier: 't2' })).toBe('m2|c2')
  })
  it('reads usage routed_model/model + routed_tier', () => {
    expect(routerFxUsageIdentity({ routed_model: 'M', routed_tier: 't3' })).toBe('m|c3')
    expect(routerFxUsageIdentity({ model: 'M', routed_tier: 't0' })).toBe('m|c0')
  })
  it('returns "" for a nullish or non-object arg', () => {
    expect(routerFxDecisionIdentity(null)).toBe('')
    expect(routerFxUsageIdentity(undefined)).toBe('')
  })
})

/* ── routerFxVisualEntries / routerFxHasMultipleCandidates (parity chat.js:3508/3551) ── */

function seededRegistry() {
  const reg = createRouterFxRegistry()
  // Mimic _loadFeatureToggles populating tier configs from config.
  reg.rememberTierDecision('c1', 'anthropic/claude-a')
  reg.rememberTierDecision('c2', 'openai/gpt-b')
  reg.rememberTierDecision('c3', 'z-ai/glm-c')
  reg.setConfigTiers(new Set(['c1', 'c2', 'c3']))
  return reg
}

describe('routerFxVisualEntries (parity chat.js:3508)', () => {
  it('returns [] until config tiers are known', () => {
    const reg = createRouterFxRegistry()
    expect(routerFxVisualEntries(reg, 'text', null)).toEqual([])
  })
  it('builds one entry per distinct display model for a text request', () => {
    const reg = seededRegistry()
    const entries = routerFxVisualEntries(reg, 'text', null)
    expect(entries.map((e) => e.displayName).sort()).toEqual(['claude-a', 'glm-c', 'gpt-b'])
  })
  it('folds the decision tier/model into the roster', () => {
    const reg = seededRegistry()
    const entries = routerFxVisualEntries(reg, 'text', { tier: 'c2', model: 'openai/gpt-b' })
    // gpt-b already present via c2 — deduped by display name, no extra cell
    expect(entries.filter((e) => e.displayName === 'gpt-b')).toHaveLength(1)
  })
  it('excludes image-only tiers from a text request', () => {
    const reg = createRouterFxRegistry()
    reg.rememberTierDecision('c1', 'text-model')
    reg.setTierConfig('c2', { model: 'img-model', supportsImage: false, imageOnly: true })
    reg.setConfigTiers(new Set(['c1', 'c2']))
    const entries = routerFxVisualEntries(reg, 'text', null)
    expect(entries.map((e) => e.displayName)).toEqual(['text-model'])
  })
})

describe('routerFxHasMultipleCandidates (parity chat.js:3551)', () => {
  it('is true when more than one candidate exists', () => {
    const reg = seededRegistry()
    expect(routerFxHasMultipleCandidates(reg, 'text', null)).toBe(true)
  })
  it('is false with a single candidate', () => {
    const reg = createRouterFxRegistry()
    reg.rememberTierDecision('c1', 'only-model')
    reg.setConfigTiers(new Set(['c1']))
    expect(routerFxHasMultipleCandidates(reg, 'text', null)).toBe(false)
  })
})

/* ── createRouterFxRenderer factory smoke (branch-level, no layout) ── */

function makeRenderer(reg: RouterFxRegistry, thread: HTMLElement | null) {
  return createRouterFxRenderer({
    thread: () => thread,
    dock: () => null, // this isolated factory test intentionally has no dock
    getSessionKey: () => 'agent:main:webchat:default',
    registry: reg,
    pref: { enabled: true, variant: 'default' },
    routerFeatureEnabled: () => true,
    esc: (s) => s,
    scrollToBottom: () => {},
  })
}

describe('createRouterFxRenderer (factory smoke)', () => {
  it('buildRouterFxElement returns null with <= 1 candidate (chat.js:3749)', () => {
    const reg = createRouterFxRegistry()
    reg.rememberTierDecision('c1', 'only-model')
    reg.setConfigTiers(new Set(['c1']))
    const renderer = makeRenderer(reg, document.createElement('div'))
    expect(renderer.buildRouterFxElement({ tier: 'c1', model: 'only-model' })).toBeNull()
  })

  it('buildRouterFxElement builds a .router-fx grid for >1 candidate', () => {
    const reg = seededRegistry()
    const renderer = makeRenderer(reg, document.createElement('div'))
    const el = renderer.buildRouterFxElement({ tier: 'c1', model: 'anthropic/claude-a' })
    expect(el).not.toBeNull()
    expect(el!.classList.contains('router-fx')).toBe(true)
    expect(el!.querySelectorAll('.router-fx-cell').length).toBe(3)
    expect(el!.querySelector('.router-fx-header .title')).toHaveTextContent('Choosing a model')
    expect(el).toHaveAttribute('role', 'status')
    expect(el).toHaveAttribute('aria-live', 'polite')
    expect(el).toHaveAttribute('aria-atomic', 'true')
  })

  it('uses friendly selected-model copy for a pre-settled strip', () => {
    const reg = seededRegistry()
    const renderer = makeRenderer(reg, document.createElement('div'))
    const el = renderer.buildRouterFxElement(
      { tier: 'c1', model: 'anthropic/claude-a' },
      { preSettled: true },
    )
    expect(el).not.toBeNull()
    expect(el!.dataset.state).toBe('settled')
    expect(el!.dataset.hasWinner).toBe('true')
    expect(el!.querySelector('.router-fx-header .title')).toHaveTextContent('Model selected')
    expect(el).toHaveAttribute('aria-label', 'Model selected: claude-a')
  })

  it('describes observe-mode output as a suggestion rather than a selection', () => {
    const reg = seededRegistry()
    const renderer = makeRenderer(reg, document.createElement('div'))
    const el = renderer.buildRouterFxElement(
      { tier: 'c1', model: 'anthropic/claude-a', routing_applied: false },
      { preSettled: true },
    )
    expect(el).not.toBeNull()
    expect(el!.querySelector('.router-fx-header .title')).toHaveTextContent('Suggested model')
    expect(el).toHaveAttribute('aria-label', 'Suggested model: claude-a')
  })

  it('never announces a selection when a scan finishes without a winner', () => {
    vi.useFakeTimers()
    const reg = seededRegistry()
    const host = document.createElement('div')
    const thread = document.createElement('div')
    const anchor = document.createElement('div')
    const dock = document.createElement('div')
    anchor.className = 'msg user'
    thread.appendChild(anchor)
    host.append(thread, dock)
    document.body.appendChild(host)
    const renderer = createRouterFxRenderer({
      thread: () => thread,
      dock: () => dock,
      getSessionKey: () => 's',
      registry: reg,
      pref: { enabled: true, variant: 'default' },
      routerFeatureEnabled: () => true,
      esc: (s) => s,
      scrollToBottom: () => {},
    })

    try {
      expect(renderer.beginScan(anchor, 'seed')).toBe(true)
      vi.advanceTimersByTime(601)
      const el = dock.querySelector('.router-fx') as HTMLElement
      expect(el.dataset.state).toBe('settled')
      expect(el.dataset.hasWinner).toBe('false')
      expect(el.querySelector('.router-fx-header .title')).toHaveTextContent('Finalizing model')
      expect(el.getAttribute('aria-label')).not.toContain('selected')
    } finally {
      renderer.clearRouterFxVisuals()
      host.remove()
      vi.useRealTimers()
    }
  })

  it('hasDock reflects the injected dock element (chat.js `if (_routerFxDock)`)', () => {
    const reg = seededRegistry()
    const withoutDock = makeRenderer(reg, document.createElement('div'))
    expect(withoutDock.hasDock()).toBe(false)
    const dockEl = document.createElement('div')
    const withDock = createRouterFxRenderer({
      thread: () => document.createElement('div'),
      dock: () => dockEl,
      getSessionKey: () => 's',
      registry: reg,
      pref: { enabled: true, variant: 'default' },
      routerFeatureEnabled: () => true,
      esc: (s) => s,
      scrollToBottom: () => {},
    })
    expect(withDock.hasDock()).toBe(true)
  })

  it('handleRouterDecision is a safe no-op when the pref is disabled', async () => {
    const reg = seededRegistry()
    const renderer = createRouterFxRenderer({
      thread: () => document.createElement('div'),
      dock: () => document.createElement('div'),
      getSessionKey: () => 's',
      registry: reg,
      pref: { enabled: false, variant: 'default' },
      routerFeatureEnabled: () => true,
      esc: (s) => s,
      scrollToBottom: () => {},
    })
    // tier is remembered even when the visualisation is off (warm cache), but no
    // strip is mounted (dock has no children). Must not throw.
    await renderer.handleRouterDecision({ tier: 'c1', model: 'anthropic/claude-a' })
    expect(reg.models['c1']).toBe('anthropic/claude-a')
  })

  it('handleRouterDecision skips a payload with no tier (chat.js:4486)', async () => {
    const reg = seededRegistry()
    const renderer = makeRenderer(reg, document.createElement('div'))
    await expect(renderer.handleRouterDecision({ model: 'x' })).resolves.toBeUndefined()
  })
})
