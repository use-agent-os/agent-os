import { describe, expect, it } from 'vitest'
import type { RawSkill, RegistryItem } from '@/views/skills/logic'
import {
  acquisitionSourceLabel,
  forgetSessionInstall,
  HIDDEN_COMMUNITY_SOURCES,
  installedAtLabel,
  isPartnerBrand,
  isRegistrySource,
  originLine,
  panelSubtitle,
  partnerBrandOf,
  reqStatusTone,
  requirementDetail,
  resolveSelection,
  sessionInstallsFor,
  SKILL_SOURCES,
  skillPrefillText,
  skillRowKey,
  verdictTone,
} from './logic'

describe('sources', () => {
  it('lists Installed first and Community last, one entry per catalog', () => {
    expect(SKILL_SOURCES[0]).toBe('installed')
    expect(SKILL_SOURCES[SKILL_SOURCES.length - 1]).toBe('community')
    expect(new Set(SKILL_SOURCES).size).toBe(SKILL_SOURCES.length)
  })

  it('treats only the remote catalogs as registry sources', () => {
    expect(isRegistrySource('installed')).toBe(false)
    expect(isRegistrySource('robinhood')).toBe(false)
    expect(isRegistrySource('bankr')).toBe(true)
    expect(isRegistrySource('community')).toBe(true)
  })

  it('hides every partner that owns a rail entry from Community', () => {
    expect([...HIDDEN_COMMUNITY_SOURCES].sort()).toEqual(['aeon', 'bankr', 'capminal'])
    expect(HIDDEN_COMMUNITY_SOURCES.has('robinhood')).toBe(false)
  })
})

describe('partner brand', () => {
  it('reads publisher.id and nothing else', () => {
    expect(partnerBrandOf({ publisher: { id: 'Bankr' } })).toBe('bankr')
    expect(partnerBrandOf({ name: 'bankr-wallet', homepage: 'https://bankr.bot' })).toBeNull()
    expect(partnerBrandOf({ publisher: { id: 'someone-else' } })).toBeNull()
  })

  it('accepts exactly the four bundled brands', () => {
    expect(['robinhood', 'bankr', 'aeon', 'capminal'].every(isPartnerBrand)).toBe(true)
    expect(isPartnerBrand('gmgn')).toBe(false)
  })
})

describe('selection', () => {
  const rows = [{ k: 'a' }, { k: 'b' }, { k: 'c' }]
  const keyOf = (r: { k: string }) => r.k

  it('keeps the current row while it is still visible', () => {
    expect(resolveSelection(rows, keyOf, 'b')).toEqual({ k: 'b' })
  })

  it('falls back to the first row when the selection was filtered out', () => {
    expect(resolveSelection(rows, keyOf, 'zzz')).toEqual({ k: 'a' })
    expect(resolveSelection(rows, keyOf, null)).toEqual({ k: 'a' })
  })

  it('is null only for an empty list', () => {
    expect(resolveSelection([], keyOf, 'a')).toBeNull()
  })

  it('keys skills by name', () => {
    expect(skillRowKey({ name: 'x-research' })).toBe('x-research')
    expect(skillRowKey({})).toBe('')
  })
})

describe('use in chat', () => {
  it('names the skill and leaves the caret on a fresh line', () => {
    expect(skillPrefillText('x-research')).toBe('use skill x-research\n')
  })
})

describe('provenance copy', () => {
  it('prefers the acquisition kind over the loading layer', () => {
    expect(acquisitionSourceLabel({ acquisition: { kind: 'hub', source_id: 'clawhub' } })).toBe(
      'clawhub',
    )
    expect(acquisitionSourceLabel({ acquisition: { kind: 'shipped' }, layer: 'managed' })).not.toBe(
      'managed',
    )
  })

  it('falls back to the layer on a pre-acquisition gateway', () => {
    expect(acquisitionSourceLabel({ layer: 'managed' })).toBe('managed')
  })

  it('names the hub and the author only for hub installs', () => {
    expect(
      originLine({ acquisition: { kind: 'hub', source_id: 'clawhub', author: '@igor' } }),
    ).toBe('clawhub · @igor')
    expect(originLine({ acquisition: { kind: 'hub' } })).toBe('hub')
    expect(originLine({ acquisition: { kind: 'shipped' } })).toBe('')
  })
})

describe('requirements', () => {
  it('lists what is missing first', () => {
    const d = requirementDetail({
      missing_bins: ['jq'],
      missing_env: ['API_KEY'],
      requires_bins: ['jq'],
    })
    expect(d.missing).toEqual(['jq', 'API_KEY'])
    expect(d.text.length).toBeGreaterThan(0)
  })

  it('describes the declaration when nothing is missing', () => {
    const d = requirementDetail({ requires_bins: ['node'], requires_any_bins: ['npm', 'pnpm'] })
    expect(d.missing).toEqual([])
    expect(d.text).toContain('node')
    expect(d.text).toContain('npm / pnpm')
  })

  it('maps statuses to tones: ready ok, problems warn, undeclared neutral', () => {
    expect(reqStatusTone('ready')).toBe('ok')
    expect(reqStatusTone('needs_setup')).toBe('warn')
    expect(reqStatusTone('missing_skill')).toBe('warn')
    expect(reqStatusTone('not_declared')).toBeUndefined()
  })
})

describe('session installs', () => {
  const rows: RegistryItem[] = [
    { identifier: 'a', name: 'a', source: 'bankr' },
    { identifier: 'b', name: 'b', source: 'clawhub' },
    { identifier: 'c', name: 'c', source: 'aeon' },
  ]

  it('gives a partner catalog only its own rows', () => {
    expect(sessionInstallsFor('bankr', rows).map((r) => r.identifier)).toEqual(['a'])
  })

  it('gives Community everything that has no rail entry of its own', () => {
    expect(sessionInstallsFor('community', rows).map((r) => r.identifier)).toEqual(['b'])
  })

  it('forgets a removed skill by name or identifier', () => {
    expect(forgetSessionInstall(rows, 'b', '').map((r) => r.identifier)).toEqual(['a', 'c'])
    expect(forgetSessionInstall(rows, '', 'c').map((r) => r.identifier)).toEqual(['a', 'b'])
  })
})

describe('header and facts', () => {
  it('summarises counts and only mentions setup when something needs it', () => {
    expect(panelSubtitle(0, 0, 0)).toBe('')
    expect(panelSubtitle(1, 1, 0)).toBe('1 skill · 1 ready')
    expect(panelSubtitle(12, 9, 3)).toBe('12 skills · 9 ready · 3 need setup')
  })

  it('tones scan verdicts without inventing one for unknown words', () => {
    expect(verdictTone('clean')).toBe('ok')
    expect(verdictTone('suspicious')).toBe('warn')
    expect(verdictTone('dangerous')).toBe('danger')
    expect(verdictTone('whatever')).toBeUndefined()
    expect(verdictTone(undefined)).toBeUndefined()
  })

  it('formats an install date and swallows garbage', () => {
    expect(installedAtLabel('2026-09-01T10:00:00Z')).toMatch(/2026/)
    expect(installedAtLabel('not a date')).toBe('')
    expect(installedAtLabel(undefined)).toBe('')
  })
})

describe('type shape', () => {
  it('accepts a bare skill row', () => {
    const s: RawSkill = { name: 'x' }
    expect(skillRowKey(s)).toBe('x')
  })
})
