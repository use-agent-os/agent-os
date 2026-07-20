import { describe, expect, it } from 'vitest'
import {
  REGISTRY_SEARCH_DEBOUNCE_MS,
  categoriesFor,
  categoryChips,
  communityFilter,
  filterRegistry,
  filterSkills,
  firstUpdateResult,
  groupSkillsByLayer,
  initials,
  installAction,
  installSource,
  installedEmptyMessage,
  isRobinhoodSkill,
  layerHelp,
  layerLabel,
  markInstalled,
  registryEmptyMessage,
  registryKey,
  robinhoodSkills,
  safeUrl,
  skillDotClass,
  skillDotTitle,
  skillRank,
  skillStats,
  skillStatus,
  stillMissingCount,
  type RawSkill,
  type RegistryItem,
} from './logic'

const skill = (o: Partial<RawSkill>): RawSkill => o

describe('layerLabel / layerHelp', () => {
  it('maps known layers and falls back for unknown', () => {
    expect(layerLabel('managed')).toBe('Managed')
    expect(layerLabel('mystery')).toBe('mystery')
    expect(layerLabel(undefined)).toBe('Unknown')
    expect(layerHelp('bundled')).toBe('Bundled skills ship with AgentOS.')
    expect(layerHelp('mystery')).toBe('Configured local skill directory.')
  })
})

describe('skillStats', () => {
  it('counts total / ready / needs_setup / not_declared', () => {
    const list = [
      skill({ status: 'ready' }),
      skill({ status: 'ready' }),
      skill({ status: 'needs_setup' }),
      skill({ status: 'not_declared' }),
      skill({ status: 'other' }),
    ]
    expect(skillStats(list)).toEqual({ total: 5, ready: 2, needs: 1, notDeclared: 1 })
  })
})

describe('filterSkills', () => {
  const list = [
    skill({ name: 'alpha', description: 'Trading bot', status: 'ready', triggers: ['buy'] }),
    skill({ name: 'beta', description: 'wallet things', status: 'needs_setup' }),
    skill({ name: 'gamma', status: 'not_declared', triggers: ['plot charts'] }),
  ]

  it('filters by name, description, and triggers (case-insensitive)', () => {
    expect(filterSkills(list, 'ALPHA', 'all').map((s) => s.name)).toEqual(['alpha'])
    expect(filterSkills(list, 'wallet', 'all').map((s) => s.name)).toEqual(['beta'])
    expect(filterSkills(list, 'charts', 'all').map((s) => s.name)).toEqual(['gamma'])
  })

  it('applies the status filter (needs-setup maps to needs_setup)', () => {
    expect(filterSkills(list, '', 'ready').map((s) => s.name)).toEqual(['alpha'])
    expect(filterSkills(list, '', 'needs-setup').map((s) => s.name)).toEqual(['beta'])
    expect(filterSkills(list, '', 'not-declared').map((s) => s.name)).toEqual(['gamma'])
    expect(filterSkills(list, '', 'all')).toHaveLength(3)
  })

  it('combines text and status filters', () => {
    expect(filterSkills(list, 'a', 'ready').map((s) => s.name)).toEqual(['alpha'])
  })
})

describe('installedEmptyMessage', () => {
  it('prefers the filter message, then status, then default', () => {
    expect(installedEmptyMessage('xyz', 'all')).toContain('xyz')
    expect(installedEmptyMessage('', 'ready')).toContain('No skills are ready')
    expect(installedEmptyMessage('', 'needs-setup')).toContain('need setup')
    expect(installedEmptyMessage('', 'not-declared')).toContain('without declared')
    expect(installedEmptyMessage('', 'all')).toBe('No skills installed.')
  })
})

describe('skillRank / groupSkillsByLayer', () => {
  it('ranks ready < not_declared < needs_setup', () => {
    expect(skillRank(skill({ status: 'ready' }))).toBe(0)
    expect(skillRank(skill({ status: 'not_declared' }))).toBe(1)
    expect(skillRank(skill({ status: 'needs_setup' }))).toBe(2)
    expect(skillRank(skill({ status: 'weird' }))).toBe(2)
  })

  it('buckets by layer in LAYER_ORDER, sorts ready-first then name, drops empties', () => {
    const list = [
      skill({ name: 'z-ready', layer: 'managed', status: 'ready' }),
      skill({ name: 'a-needs', layer: 'managed', status: 'needs_setup' }),
      skill({ name: 'm-decl', layer: 'managed', status: 'not_declared' }),
      skill({ name: 'b', layer: 'bundled', status: 'ready' }),
      skill({ name: 'x', status: 'ready' }), // no layer → extra
    ]
    const groups = groupSkillsByLayer(list)
    // bundled before managed before extra (LAYER_ORDER)
    expect(groups.map((g) => g.layer)).toEqual(['bundled', 'managed', 'extra'])
    // managed: ready(0) then not_declared(1) then needs_setup(2)
    const managed = groups.find((g) => g.layer === 'managed')!
    expect(managed.skills.map((s) => s.name)).toEqual(['z-ready', 'm-decl', 'a-needs'])
    expect(managed.label).toBe('Managed')
  })

  it('returns [] for no skills', () => {
    expect(groupSkillsByLayer([])).toEqual([])
  })
})

describe('skillStatus / skillDotClass / skillDotTitle', () => {
  it('falls back to eligible when status absent', () => {
    expect(skillStatus(skill({ eligible: true }))).toBe('ready')
    expect(skillStatus(skill({ eligible: false }))).toBe('needs_setup')
    expect(skillStatus(skill({ status: 'not_declared' }))).toBe('not_declared')
  })

  it('maps status to dot class', () => {
    expect(skillDotClass(skill({ status: 'ready' }))).toBe('is-ready')
    expect(skillDotClass(skill({ status: 'needs_setup' }))).toBe('is-needs')
    expect(skillDotClass(skill({ status: 'not_declared' }))).toBe('is-unverified')
  })

  it('dot title prefers status_detail then eligible label', () => {
    expect(skillDotTitle(skill({ status_detail: 'Custom' }))).toBe('Custom')
    expect(skillDotTitle(skill({ eligible: true }))).toBe('Ready')
    expect(skillDotTitle(skill({ eligible: false }))).toBe('Needs setup')
  })
})

describe('isRobinhoodSkill / robinhoodSkills', () => {
  it('only bundled skills named robinhood* or homepaged robinhood.com qualify', () => {
    expect(isRobinhoodSkill(skill({ layer: 'bundled', name: 'robinhood-stocks' }))).toBe(true)
    expect(
      isRobinhoodSkill(skill({ layer: 'bundled', name: 'x', homepage: 'https://robinhood.com/x' })),
    ).toBe(true)
    // not bundled → excluded even if named robinhood
    expect(isRobinhoodSkill(skill({ layer: 'managed', name: 'robinhood-fake' }))).toBe(false)
    // bundled but unrelated → excluded
    expect(isRobinhoodSkill(skill({ layer: 'bundled', name: 'weather' }))).toBe(false)
  })

  it('robinhoodSkills filters + sorts by name', () => {
    const list = [
      skill({ layer: 'bundled', name: 'robinhood-z' }),
      skill({ layer: 'bundled', name: 'robinhood-a' }),
      skill({ layer: 'managed', name: 'robinhood-nope' }),
    ]
    expect(robinhoodSkills(list).map((s) => s.name)).toEqual(['robinhood-a', 'robinhood-z'])
  })
})

const item = (o: Partial<RegistryItem>): RegistryItem => o

describe('communityFilter', () => {
  const rows = [item({ source: 'bankr', name: 'b' }), item({ source: 'clawhub', name: 'c' })]
  it('drops bankr rows when the Bankr tab is shown', () => {
    expect(communityFilter(rows, true).map((r) => r.name)).toEqual(['c'])
  })
  it('keeps bankr rows when the Bankr tab is hidden', () => {
    expect(communityFilter(rows, false).map((r) => r.name)).toEqual(['b', 'c'])
  })
})

describe('categoriesFor / categoryChips', () => {
  const rows = [
    item({ category: 'trading' }),
    item({ category: 'trading' }),
    item({ category: 'defi' }),
    item({}), // → other
  ]
  it('counts categories with other fallback', () => {
    expect(categoriesFor(rows)).toEqual({ trading: 2, defi: 1, other: 1 })
  })

  it('builds chips: all first, then count-desc; marks active', () => {
    const chips = categoryChips(rows, 'defi')
    expect(chips[0]!).toMatchObject({ cat: 'all', count: 4 })
    expect(chips[1]!).toMatchObject({ cat: 'trading', count: 2 })
    const defi = chips.find((c) => c.cat === 'defi')
    expect(defi?.active).toBe(true)
    expect(chips.find((c) => c.cat === 'all')?.active).toBe(false)
  })

  it('returns no chips when only the other category is present', () => {
    expect(categoryChips([item({}), item({})], 'all')).toEqual([])
  })

  it('returns no chips for an empty snapshot', () => {
    expect(categoryChips([], 'all')).toEqual([])
  })
})

describe('filterRegistry', () => {
  const rows = [
    item({ name: 'Swap', provider: 'Uniswap', description: 'DEX', category: 'defi' }),
    item({ name: 'Buy', provider: 'Bankr', description: 'trade', category: 'trading' }),
  ]
  it('filters by category', () => {
    expect(filterRegistry(rows, 'defi', '').map((r) => r.name)).toEqual(['Swap'])
    expect(filterRegistry(rows, 'all', '')).toHaveLength(2)
  })
  it('filters by text over name/provider/description', () => {
    expect(filterRegistry(rows, 'all', 'uniswap').map((r) => r.name)).toEqual(['Swap'])
    expect(filterRegistry(rows, 'all', 'trade').map((r) => r.name)).toEqual(['Buy'])
  })
  it('combines category and text', () => {
    expect(filterRegistry(rows, 'trading', 'buy').map((r) => r.name)).toEqual(['Buy'])
    expect(filterRegistry(rows, 'defi', 'buy')).toEqual([])
  })
})

describe('registryEmptyMessage / registryKey', () => {
  it('query message takes precedence', () => {
    expect(registryEmptyMessage('bankr', 'foo')).toContain('foo')
    expect(registryEmptyMessage('bankr', '')).toContain('Bankr')
    expect(registryEmptyMessage('community', '')).toContain('community')
  })
  it('registryKey prefers identifier then name', () => {
    expect(registryKey(item({ identifier: 'id1', name: 'n' }))).toBe('id1')
    expect(registryKey(item({ name: 'n' }))).toBe('n')
  })
})

describe('installAction / installSource', () => {
  it('installed rows show the installed badge', () => {
    expect(installAction(item({ installed: true }), new Set())).toBe('installed')
  })
  it('force-armed rows show a force install', () => {
    expect(installAction(item({ identifier: 'x' }), new Set(['x']))).toBe('force')
  })
  it('otherwise a normal install', () => {
    expect(installAction(item({ identifier: 'x' }), new Set())).toBe('install')
  })
  it('installSource defaults to clawhub', () => {
    expect(installSource(item({}))).toBe('clawhub')
    expect(installSource(item({ source: 'bankr' }))).toBe('bankr')
  })
})

describe('stillMissingCount', () => {
  it('sums missing bins + env', () => {
    expect(stillMissingCount({ missing_still: { bins: ['a'], env: ['B', 'C'] } })).toBe(3)
    expect(stillMissingCount({})).toBe(0)
  })
})

describe('firstUpdateResult', () => {
  it('unwraps the first result, defaulting to {}', () => {
    expect(firstUpdateResult({ results: [{ success: true, message: 'ok' }] })).toEqual({
      success: true,
      message: 'ok',
    })
    expect(firstUpdateResult({})).toEqual({})
    expect(firstUpdateResult({ results: [] })).toEqual({})
  })
})

describe('initials / safeUrl', () => {
  it('takes first letters of the first two words', () => {
    expect(initials('Uniswap Labs')).toBe('UL')
    expect(initials('Bankr')).toBe('B')
    expect(initials('   ')).toBe('?')
  })
  it('safeUrl only passes http(s)', () => {
    expect(safeUrl('https://x.com')).toBe('https://x.com')
    expect(safeUrl('http://x.com')).toBe('http://x.com')
    expect(safeUrl('javascript:alert(1)')).toBe('')
    expect(safeUrl(undefined)).toBe('')
  })
})

describe('markInstalled', () => {
  it('flips installed on matching identifier or name, immutably', () => {
    const list = [
      item({ identifier: 'id1', name: 'a', installed: false }),
      item({ name: 'b', installed: false }),
    ]
    const flipped = markInstalled(list, 'id1', '', true)
    expect(flipped[0]!.installed).toBe(true)
    expect(flipped[1]!.installed).toBe(false)
    // original untouched
    expect(list[0]!.installed).toBe(false)
    // match by name
    const byName = markInstalled(list, '', 'b', true)
    expect(byName[1]!.installed).toBe(true)
  })
})

describe('REGISTRY_SEARCH_DEBOUNCE_MS', () => {
  it('is the legacy 250ms interval', () => {
    expect(REGISTRY_SEARCH_DEBOUNCE_MS).toBe(250)
  })
})
