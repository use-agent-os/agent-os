import { describe, expect, it } from 'vitest'
import {
  REGISTRY_SEARCH_DEBOUNCE_MS,
  categoriesFor,
  categoryChips,
  communityFilter,
  filterRegistry,
  filterSkills,
  firstUpdateResult,
  groupSkills,
  initials,
  installAction,
  installSource,
  installedEmptyMessage,
  isGmgnSkill,
  isPartnerSkill,
  layerHelp,
  layerLabel,
  markInstalled,
  mergeRegistryRows,
  partnerEmptyMessage,
  registryEmptyMessage,
  registryKey,
  safeUrl,
  skillAvailabilityLabel,
  skillAvailabilityTitle,
  skillAvailabilityTone,
  skillDotClass,
  skillDotTitle,
  skillCanRemove,
  skillCanUpdate,
  skillGroupKey,
  skillPublisherId,
  skillRank,
  skillStats,
  skillStatus,
  skillsByPublisher,
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
  it('counts total / ready / needs_setup / disabled', () => {
    const list = [
      skill({ status: 'ready' }),
      skill({ status: 'ready' }),
      skill({ status: 'needs_setup' }),
      skill({ status: 'not_declared' }),
      skill({ status: 'other' }),
    ]
    // ready + not_declared + an unreadable status all run → all count as ready.
    expect(skillStats(list)).toEqual({ total: 5, ready: 4, needs: 1, disabled: 0 })
  })

  // A skill with no `requires:` block runs; the wire keeps `not_declared`
  // separate only to record that AgentOS verified nothing. Surfacing that as
  // its own bucket read as a defect and no action ever followed it.
  it('counts an undeclared skill as ready, not as a bucket of its own', () => {
    const list = [skill({ status: 'ready' }), skill({ status: 'not_declared' })]
    expect(skillStats(list)).toMatchObject({ total: 2, ready: 2, needs: 0 })
  })

  // The wire folds "switched off" into needs_setup. Counting it there tells an
  // operator to go install something for a skill they turned off themselves.
  it('counts a disabled skill as disabled, not as needing setup', () => {
    const list = [
      skill({ status: 'needs_setup', disabled: true }),
      skill({ status: 'needs_setup' }),
    ]
    expect(skillStats(list)).toMatchObject({ needs: 1, disabled: 1 })
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

  // `not_declared` means the skill runs and declared nothing to verify, so it
  // filters as Ready alongside a skill whose declared deps are all satisfied.
  it('applies the status filter, counting an undeclared skill as ready', () => {
    expect(filterSkills(list, '', 'ready').map((s) => s.name)).toEqual(['alpha', 'gamma'])
    expect(filterSkills(list, '', 'needs-setup').map((s) => s.name)).toEqual(['beta'])
    expect(filterSkills(list, '', 'all')).toHaveLength(3)
  })

  it('combines text and status filters', () => {
    expect(filterSkills(list, 'a', 'ready').map((s) => s.name)).toEqual(['alpha', 'gamma'])
    expect(filterSkills(list, 'a', 'needs-setup').map((s) => s.name)).toEqual(['beta'])
  })
})

describe('installedEmptyMessage', () => {
  it('prefers the filter message, then status, then default', () => {
    expect(installedEmptyMessage('xyz', 'all')).toContain('xyz')
    expect(installedEmptyMessage('', 'ready')).toContain('No skills are ready')
    expect(installedEmptyMessage('', 'needs-setup')).toContain('need setup')
    expect(installedEmptyMessage('', 'disabled')).toContain('switched off')
    expect(installedEmptyMessage('', 'all')).toBe('No skills installed.')
  })
})

const acquired = (kind: string, o: Partial<RawSkill> = {}): RawSkill =>
  skill({ acquisition: { kind }, ...o })

describe('skillRank / skillGroupKey / groupSkills', () => {
  it('ranks ready < disabled < needs_setup', () => {
    expect(skillRank(skill({ status: 'ready' }))).toBe(0)
    expect(skillRank(skill({ status: 'not_declared' }))).toBe(0)
    expect(skillRank(skill({ status: 'needs_setup', disabled: true }))).toBe(1)
    expect(skillRank(skill({ status: 'needs_setup' }))).toBe(2)
    // An unreadable status is "nothing to report", not a warning to act on.
    expect(skillRank(skill({ status: 'weird' }))).toBe(0)
  })

  it('maps acquisition.kind to a group key', () => {
    expect(skillGroupKey(acquired('shipped'))).toBe('shipped')
    expect(skillGroupKey(acquired('hub'))).toBe('hub')
    expect(skillGroupKey(acquired('local'))).toBe('local')
  })

  it('partners wins over acquisition, whichever kind it is', () => {
    const shipped = acquired('shipped', { publisher: { id: 'robinhood', name: 'Robinhood' } })
    const hub = acquired('hub', { publisher: { id: 'bankr', name: 'Bankr' } })
    expect(skillGroupKey(shipped)).toBe('partners')
    expect(skillGroupKey(hub)).toBe('partners')
  })

  it('falls back to layer when a pre-#130 gateway sends no acquisition', () => {
    expect(skillGroupKey(skill({ layer: 'bundled' }))).toBe('shipped')
    expect(skillGroupKey(skill({ layer: 'managed' }))).toBe('hub')
    expect(skillGroupKey(skill({ layer: 'project' }))).toBe('local')
    expect(skillGroupKey(skill({}))).toBe('local')
  })

  it('splits crypto out of shipped, and only for shipped skills', () => {
    expect(skillGroupKey(acquired('shipped', { category: 'crypto' }))).toBe('crypto')
    expect(skillGroupKey(skill({ layer: 'bundled', category: 'crypto' }))).toBe('crypto')
    // A user-supplied directory cannot claim an AgentOS-branded heading.
    expect(skillGroupKey(acquired('local', { category: 'crypto' }))).toBe('local')
    expect(skillGroupKey(acquired('hub', { category: 'crypto' }))).toBe('hub')
    // A partner's crypto skill still belongs to that partner.
    expect(
      skillGroupKey(acquired('shipped', { category: 'crypto', publisher: { id: 'robinhood' } })),
    ).toBe('partners')
    // Anything else stays exactly where it was.
    expect(skillGroupKey(acquired('shipped', { category: '' }))).toBe('shipped')
    expect(skillGroupKey(acquired('shipped', { category: 'CRYPTO ' }))).toBe('crypto')
  })

  it('recognises a bundled GMGN skill by provenance, never by its name', () => {
    const gmgn = { category: 'crypto', provenance: { origin: 'gmgn-mit' } }
    expect(isGmgnSkill(acquired('shipped', gmgn))).toBe(true)
    expect(isGmgnSkill(skill({ layer: 'bundled', ...gmgn }))).toBe(true)
    expect(isGmgnSkill(acquired('shipped', { ...gmgn, provenance: { origin: 'GMGN-MIT ' } }))).toBe(
      true,
    )

    // Provenance passed no allowlist, so the group key carries the trust: a
    // local or hub drop-in claiming the same origin gets no mark.
    expect(isGmgnSkill(acquired('local', gmgn))).toBe(false)
    expect(isGmgnSkill(acquired('hub', gmgn))).toBe(false)
    // A partner's skill wears its partner's mark, not this one.
    expect(isGmgnSkill(acquired('shipped', { ...gmgn, publisher: { id: 'robinhood' } }))).toBe(
      false,
    )
    // The name proves nothing either way.
    expect(isGmgnSkill(acquired('shipped', { name: 'gmgn-token', category: 'crypto' }))).toBe(false)
    // Another AgentOS crypto skill keeps the AgentOS mark.
    expect(isGmgnSkill(acquired('shipped', { category: 'crypto' }))).toBe(false)
  })

  it('groups partners → shipped → hub → local, ready-first then name, dropping empties', () => {
    const list = [
      acquired('hub', { name: 'z-ready', status: 'ready' }),
      acquired('hub', { name: 'a-needs', status: 'needs_setup' }),
      acquired('hub', { name: 'm-decl', status: 'not_declared' }),
      acquired('shipped', { name: 'b', status: 'ready' }),
      // A partner skill that ships with AgentOS and one installed from a hub
      // land under the SAME heading — the issue's acceptance criterion.
      acquired('shipped', { name: 'rh', publisher: { id: 'robinhood' }, status: 'ready' }),
      acquired('hub', { name: 'bankr-swap', publisher: { id: 'bankr' }, status: 'ready' }),
    ]
    const groups = groupSkills(list)
    expect(groups.map((g) => g.key)).toEqual(['partners', 'shipped', 'hub'])
    expect(groups[0]!.label).toBe('Partner Skills')
    expect(groups[0]!.skills.map((s) => s.name)).toEqual(['bankr-swap', 'rh'])
    expect(groups[0]!.help).toContain('partner')
    const hub = groups.find((g) => g.key === 'hub')!
    // z-ready and m-decl (not_declared) share rank 0, so name breaks the tie.
    expect(hub.skills.map((s) => s.name)).toEqual(['m-decl', 'z-ready', 'a-needs'])
    expect(hub.label).toBe('Installed from a hub')
  })

  it('emits every non-empty group in order', () => {
    const groups = groupSkills([
      acquired('local', { name: 'l' }),
      acquired('hub', { name: 'h' }),
      acquired('shipped', { name: 's' }),
      acquired('shipped', { name: 'c', category: 'crypto' }),
      acquired('shipped', { name: 'p', publisher: { id: 'bankr' } }),
    ])
    expect(groups.map((g) => g.key)).toEqual(['partners', 'crypto', 'shipped', 'hub', 'local'])
    expect(groups.map((g) => g.label)).toEqual([
      'Partner Skills',
      'AgentOS Crypto Skills',
      'AgentOS Normal Skills',
      'Installed from a hub',
      'Your local skills',
    ])
  })

  it('returns [] for no skills', () => {
    expect(groupSkills([])).toEqual([])
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
    // A skill that declared nothing still runs, so it gets the ready dot.
    expect(skillDotClass(skill({ status: 'not_declared' }))).toBe('is-ready')
    expect(skillDotClass(skill({ status: 'needs_setup', disabled: true }))).toBe('is-off')
  })

  it('dot title prefers status_detail then eligible label', () => {
    expect(skillDotTitle(skill({ status_detail: 'Custom' }))).toBe('Custom')
    expect(skillDotTitle(skill({ eligible: true }))).toBe('Ready')
    expect(skillDotTitle(skill({ eligible: false }))).toBe('Needs setup')
  })
})

describe('skillAvailability*', () => {
  it('treats an absent availability block as unknown, never as not-offered', () => {
    expect(skillAvailabilityTone(skill({}))).toBe('unknown')
    // `agentos skills list --json` omits the key entirely.
    expect(skillAvailabilityTone(skill({ status: 'ready', eligible: true }))).toBe('unknown')
    expect(skillAvailabilityLabel(skill({}))).toBe('')
  })

  it('separates offered from ready — a ready skill can still be withheld', () => {
    const withheld = skill({
      status: 'ready',
      eligible: true,
      availability: { offered: false, reason: 'model_invocation_disabled', detail: '' },
    })
    // The status derivations are untouched: it is still "ready".
    expect(skillDotClass(withheld)).toBe('is-ready')
    expect(skillAvailabilityTone(withheld)).toBe('not-offered')
    expect(skillAvailabilityLabel(withheld)).toBe('Not offered — agent cannot invoke')
  })

  it('labels each withheld reason and falls back for an unknown one', () => {
    const withReason = (reason: string) =>
      skillAvailabilityLabel(skill({ availability: { offered: false, reason } }))
    expect(withReason('ineligible')).toBe('Not offered — needs setup')
    expect(withReason('tool_gate')).toBe('Not offered — missing tools')
    expect(withReason('prompt_budget')).toBe('Not offered — prompt too long')
    expect(withReason('some_future_reason')).toBe('Not offered to the agent')
    expect(withReason('')).toBe('Not offered to the agent')
  })

  it('offered rows read as offered with an empty reason', () => {
    const offered = skill({ availability: { offered: true, reason: '', detail: '' } })
    expect(skillAvailabilityTone(offered)).toBe('offered')
    expect(skillAvailabilityLabel(offered)).toBe('Offered to the agent')
    expect(skillAvailabilityTitle(offered)).toBe('Offered to the agent')
  })

  it('the tooltip prefers the server detail prose', () => {
    const s = skill({
      availability: { offered: false, reason: 'ineligible', detail: 'Set LEDGER_API_KEY.' },
    })
    expect(skillAvailabilityTitle(s)).toBe('Set LEDGER_API_KEY.')
  })
})

describe('skillPublisherId / isPartnerSkill / skillsByPublisher', () => {
  it('trusts publisher.id and nothing else', () => {
    expect(skillPublisherId(skill({ publisher: { id: 'Robinhood' } }))).toBe('robinhood')
    expect(skillPublisherId(skill({ publisher: { id: ' bankr ' } }))).toBe('bankr')
    expect(skillPublisherId(skill({}))).toBe('')
    expect(skillPublisherId(skill({ publisher: { id: '', name: 'Robinhood' } }))).toBe('')
  })

  it('a robinhood-* name with no publisher is NOT a partner skill', () => {
    // The old heuristic read the name and the homepage; the allowlist that
    // replaced it lives server-side, and the client must not re-derive a brand.
    const impostor = skill({
      layer: 'bundled',
      name: 'robinhood-stocks',
      homepage: 'https://robinhood.com/x',
    })
    expect(isPartnerSkill(impostor)).toBe(false)
    expect(skillsByPublisher([impostor], 'robinhood')).toEqual([])
    expect(skillGroupKey(impostor)).toBe('shipped')
  })

  it('an unbranded publisher block reads as unbranded', () => {
    // The server flattens an unrecognized id to an all-empty publisher.
    expect(isPartnerSkill(skill({ publisher: { id: '', name: '', url: '', logo: '' } }))).toBe(
      false,
    )
  })

  it('selects one publisher and sorts by name', () => {
    const list = [
      skill({ name: 'rh-z', publisher: { id: 'robinhood' } }),
      skill({ name: 'rh-a', publisher: { id: 'robinhood' } }),
      skill({ name: 'bankr-swap', publisher: { id: 'bankr' } }),
      skill({ name: 'robinhood-impostor' }),
    ]
    expect(skillsByPublisher(list, 'robinhood').map((s) => s.name)).toEqual(['rh-a', 'rh-z'])
    expect(skillsByPublisher(list, 'bankr').map((s) => s.name)).toEqual(['bankr-swap'])
    // An empty id never selects the unbranded rows.
    expect(skillsByPublisher(list, '')).toEqual([])
  })
})

describe('skillCanUpdate / skillCanRemove', () => {
  it('reads the affordance off acquisition, not the layer', () => {
    const movedHubInstall = acquired('hub', { layer: 'workspace' })
    movedHubInstall.acquisition!.updatable = true
    movedHubInstall.acquisition!.removable = true
    expect(skillCanUpdate(movedHubInstall)).toBe(true)
    expect(skillCanRemove(movedHubInstall)).toBe(true)

    // A hand-copied directory sitting in the managed dir was never removable.
    const handCopied = acquired('local', { layer: 'managed' })
    expect(skillCanUpdate(handCopied)).toBe(false)
    expect(skillCanRemove(handCopied)).toBe(false)
  })

  it('a hub install whose files moved is updatable but not removable', () => {
    const diverged = acquired('hub')
    diverged.acquisition!.updatable = true
    diverged.acquisition!.removable = false
    expect(skillCanUpdate(diverged)).toBe(true)
    expect(skillCanRemove(diverged)).toBe(false)
  })

  it('falls back to layer when a pre-#130 gateway sends no acquisition', () => {
    // Reading the flags directly would be `undefined === true` -> false, which
    // hides both buttons on a stale bundle instead of degrading to the old gate.
    expect(skillCanUpdate(skill({ layer: 'managed' }))).toBe(true)
    expect(skillCanRemove(skill({ layer: 'managed' }))).toBe(true)
    expect(skillCanUpdate(skill({ layer: 'bundled' }))).toBe(false)
    expect(skillCanRemove(skill({ layer: 'bundled' }))).toBe(false)
  })
})

describe('partnerEmptyMessage', () => {
  it('explains query, status, and default empty states', () => {
    expect(partnerEmptyMessage('Robinhood', 'rwa', 'all')).toBe('No Robinhood skills match rwa.')
    expect(partnerEmptyMessage('Robinhood', '', 'ready')).toBe('No Robinhood skills are ready.')
    expect(partnerEmptyMessage('Bankr', '', 'needs-setup')).toBe(
      'No Bankr skills currently need setup.',
    )
    expect(partnerEmptyMessage('Bankr', '', 'disabled')).toBe('No Bankr skills are switched off.')
    expect(partnerEmptyMessage('Robinhood', '', 'all')).toContain('Robinhood skills are on the way')
  })
})

const item = (o: Partial<RegistryItem>): RegistryItem => o

describe('aeon as a partner source', () => {
  it('groups an aeon-published skill under Partners', () => {
    expect(skillGroupKey({ name: 'tx-explain', publisher: { id: 'aeon' } } as never)).toBe(
      'partners',
    )
  })

  it('does not treat an unbranded aeon-sourced skill as a partner skill', () => {
    // The brand comes from the server-side allowlist, never from the row's own
    // source string, so a synthesized row cannot land in the Partners group.
    expect(isPartnerSkill({ name: 'tx-explain', source: 'aeon' } as never)).toBe(false)
  })

  it('names Aeon in its own empty message rather than the community one', () => {
    expect(registryEmptyMessage('aeon', '')).not.toBe(registryEmptyMessage('community', ''))
  })
})

describe('communityFilter', () => {
  const rows = [
    item({ source: 'bankr', name: 'b' }),
    item({ source: 'capminal', name: 'cap' }),
    item({ source: 'clawhub', name: 'c' }),
  ]
  it('drops bankr and capminal rows when their tabs are shown', () => {
    expect(communityFilter(rows, new Set(['bankr', 'capminal'])).map((r) => r.name)).toEqual(['c'])
    expect(communityFilter(rows, new Set(['bankr'])).map((r) => r.name)).toEqual(['cap', 'c'])
    expect(communityFilter(rows, new Set(['capminal'])).map((r) => r.name)).toEqual(['b', 'c'])
  })
  it('keeps bankr and capminal rows when their tabs are hidden', () => {
    expect(communityFilter(rows, new Set()).map((r) => r.name)).toEqual(['b', 'cap', 'c'])
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

  it('also matches category, which the server matches and the client used not to', () => {
    expect(filterRegistry(rows, 'all', 'defi').map((r) => r.name)).toEqual(['Swap'])
  })

  it('skips the text pass for rows that ARE the server answer', () => {
    // The server matches over tags too, and tags are not on the wire — so a
    // legitimate hit like this one has nothing the client can match on.
    const serverHit = [item({ name: 'Swap', description: 'DEX', category: 'defi' })]
    expect(filterRegistry(serverHit, 'all', 'perpetuals')).toEqual([])
    expect(filterRegistry(serverHit, 'all', 'perpetuals', { serverFiltered: true })).toHaveLength(1)
  })

  it('still applies the category chip to server-filtered rows', () => {
    expect(
      filterRegistry(rows, 'defi', 'anything', { serverFiltered: true }).map((r) => r.name),
    ).toEqual(['Swap'])
  })
})

describe('mergeRegistryRows', () => {
  it('unions by registryKey with base winning and extras appended', () => {
    const base = [item({ identifier: 'id1', name: 'a', installed: false, description: 'rich' })]
    const extra = [
      item({ identifier: 'id1', name: 'a', installed: true }),
      item({ identifier: 'id2', name: 'b', installed: true }),
    ]
    const merged = mergeRegistryRows(base, extra)
    expect(merged.map((r) => r.identifier)).toEqual(['id1', 'id2'])
    // base wins the collision, keeping its catalog metadata
    expect(merged[0]!.description).toBe('rich')
    expect(merged[0]!.installed).toBe(false)
  })

  it('dedupes on name when a row has no identifier', () => {
    const merged = mergeRegistryRows([item({ name: 'a' })], [item({ name: 'a', installed: true })])
    expect(merged).toHaveLength(1)
    expect(merged[0]!.installed).toBeUndefined()
  })

  it('drops unkeyable extras and never mutates the inputs', () => {
    const base = [item({ name: 'a' })]
    const extra = [item({ description: 'no key' })]
    expect(mergeRegistryRows(base, extra)).toHaveLength(1)
    expect(base).toHaveLength(1)
  })

  it('handles empty inputs', () => {
    expect(mergeRegistryRows([], [])).toEqual([])
    expect(mergeRegistryRows([], [item({ name: 'a' })]).map((r) => r.name)).toEqual(['a'])
  })
})

describe('registryEmptyMessage / registryKey', () => {
  it('query message takes precedence', () => {
    expect(registryEmptyMessage('bankr', 'foo')).toContain('foo')
    expect(registryEmptyMessage('bankr', '')).toContain('Bankr')
    expect(registryEmptyMessage('capminal', '')).toContain('Capminal')
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
