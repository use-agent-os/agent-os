import { describe, expect, it } from 'vitest'
import { t } from '~/i18n'
import {
  matchSections,
  searchWords,
  sectionScore,
  SETTINGS_SECTIONS,
  type SettingsSection,
} from './sections'

/** The rail's own copy: the names and keywords people actually search. */
const describeSection = (id: SettingsSection) => ({
  name: t(`settings.section.${id}`),
  keywords: t(`settings.section.${id}.keywords`),
})
const search = (query: string) => matchSections(query, describeSection)

describe('rail search', () => {
  // What a person types → the section Return should open.
  it.each<[string, SettingsSection]>([
    ['grok', 'capabilities'],
    ['elevenlabs', 'capabilities'],
    ['x search', 'capabilities'],
    ['search x', 'capabilities'],
    ['twitter', 'capabilities'],
    ['web search', 'capabilities'],
    ['search', 'capabilities'],
    ['brave', 'capabilities'],
    ['tavily', 'capabilities'],
    ['duckduckgo', 'capabilities'],
    ['tts', 'capabilities'],
    ['text to speech', 'capabilities'],
    ['image', 'capabilities'],
    ['proxy', 'capabilities'],
    ['text size', 'appearance'],
    ['dark mode', 'appearance'],
    ['light mode', 'appearance'],
    ['color', 'appearance'],
    ['colour', 'appearance'],
    ['zoom', 'appearance'],
    ['uniswap', 'trading'],
    ['wallet', 'trading'],
    ['swap', 'trading'],
    ['vault', 'trading'],
    ['token', 'gateway'],
    ['port', 'gateway'],
    ['api key', 'providers'],
    ['openai', 'providers'],
    ['ollama', 'providers'],
    ['claude', 'providers'],
    ['telegram', 'channels'],
    ['discord', 'channels'],
    ['embedding', 'memory'],
    ['memory search', 'memory'],
    ['touch id', 'security'],
    ['lock', 'security'],
    ['sound', 'notifications'],
    ['do not disturb', 'notifications'],
    ['quick ask', 'behaviour'],
    ['behavior', 'behaviour'],
    ['keyboard', 'shortcuts'],
    ['logs', 'advanced'],
    ['version', 'about'],
    ['update', 'about'],
    ['pilot', 'router'],
    ['thinking', 'router'],
    ['.env', 'environment'],
    ['setup', 'overview'],
  ])('%s → %s', (query, expected) => {
    expect(search(query)[0]).toBe(expected)
  })

  it('every word must match, in any order, at the start of a word', () => {
    // "x" starts no word on the Memory page ("index" only contains it).
    expect(search('x search')).not.toContain('memory')
    expect(search('size text')).toEqual(search('text size'))
    expect(search('text size')).toEqual(['appearance'])
    expect(search('ndex')).toEqual([])
  })

  it('a name match ranks above a keyword match', () => {
    // "Gateway" is a section name; Behaviour only mentions it.
    expect(search('gateway')[0]).toBe('gateway')
    expect(search('gateway')).toContain('behaviour')
    expect(search('memory')[0]).toBe('memory')
  })

  it('an empty query keeps the rail order', () => {
    expect(search('   ')).toHaveLength(SETTINGS_SECTIONS.length)
    expect(search('')[0]).toBe('overview')
  })

  it('every section can be found by its own name', () => {
    for (const id of SETTINGS_SECTIONS) expect(search(describeSection(id).name)[0]).toBe(id)
  })

  it('splits on punctuation but keeps dots inside a word', () => {
    expect(searchWords('Text-to-speech, MEMORY.md (z.ai)')).toEqual([
      'text',
      'to',
      'speech',
      'memory.md',
      'z.ai',
    ])
    expect(sectionScore('nothing here', { name: 'Gateway', keywords: 'port' })).toBeNull()
  })
})
