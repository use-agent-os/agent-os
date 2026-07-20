import { describe, expect, it } from 'vitest'
import {
  LEVELS,
  DEFAULT_LEVELS,
  buildExportText,
  clampBuffer,
  countByLevel,
  extractLines,
  filterLines,
  guessLevel,
  matchesFilter,
  normalizeEntry,
  sliceTs,
  splitHighlight,
  visibleCount,
  type LogLine,
} from './logic'

describe('LEVELS / DEFAULT_LEVELS', () => {
  it('lists levels in legacy order (logs.js:10)', () => {
    expect(LEVELS).toEqual(['TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR'])
  })
  it('defaults to DEBUG/INFO/WARN/ERROR active (TRACE off, logs.js:12)', () => {
    expect([...DEFAULT_LEVELS].sort()).toEqual(['DEBUG', 'ERROR', 'INFO', 'WARN'])
    expect(DEFAULT_LEVELS.has('TRACE')).toBe(false)
  })
})

describe('guessLevel (logs.js:219-227)', () => {
  it.each([
    ['boom ERROR happened', 'ERROR'],
    ['a WARN line', 'WARN'],
    ['some INFO here', 'INFO'],
    ['DEBUG trace', 'DEBUG'],
    ['pure TRACE only', 'TRACE'],
  ])('%s -> %s', (line, level) => {
    expect(guessLevel(line)).toBe(level)
  })
  it('is case-insensitive', () => {
    expect(guessLevel('an error occurred')).toBe('ERROR')
  })
  it('prefers ERROR over lower levels when both present', () => {
    expect(guessLevel('INFO then ERROR')).toBe('ERROR')
  })
  it('defaults to INFO when no level token is found', () => {
    expect(guessLevel('nothing notable')).toBe('INFO')
  })
})

describe('extractLines (logs.js:182)', () => {
  it('reads data.lines', () => {
    expect(extractLines({ lines: [1, 2] })).toEqual([1, 2])
  })
  it('falls back to data.entries', () => {
    expect(extractLines({ entries: [3] })).toEqual([3])
  })
  it('returns [] when neither key is present', () => {
    expect(extractLines({})).toEqual([])
    expect(extractLines(null)).toEqual([])
  })
})

describe('normalizeEntry (logs.js:189-200)', () => {
  it('normalizes a string via guessLevel', () => {
    expect(normalizeEntry('a WARN line')).toEqual({
      level: 'WARN',
      message: 'a WARN line',
      raw: 'a WARN line',
    })
  })
  it('reads level (upper), message, ts, raw from an object', () => {
    expect(
      normalizeEntry({ level: 'error', message: 'boom', timestamp: '2026-01-01', raw: 'r' }),
    ).toEqual({ level: 'ERROR', message: 'boom', ts: '2026-01-01', raw: 'r' })
  })
  it('accepts lvl / msg / ts aliases', () => {
    expect(normalizeEntry({ lvl: 'warn', msg: 'hi', ts: 7 })).toMatchObject({
      level: 'WARN',
      message: 'hi',
      ts: 7,
    })
  })
  it('defaults the level to INFO', () => {
    expect(normalizeEntry({ message: 'plain' }).level).toBe('INFO')
  })
  it('falls back message to JSON of the entry and raw to JSON when raw is not a string', () => {
    const entry = { level: 'info', count: 3 }
    const line = normalizeEntry(entry)
    expect(line.message).toBe(JSON.stringify(entry))
    expect(line.raw).toBe(JSON.stringify(entry))
  })
  it('keeps a string raw verbatim', () => {
    expect(normalizeEntry({ message: 'm', raw: 'kept' }).raw).toBe('kept')
  })
  it('leaves ts undefined when absent', () => {
    expect(normalizeEntry({ message: 'm' }).ts).toBeUndefined()
  })
})

describe('clampBuffer (logs.js:201)', () => {
  it('keeps the last 2000 when over the cap', () => {
    const lines = Array.from({ length: 2500 }, (_, i) => line(`m${i}`))
    const out = clampBuffer(lines)
    expect(out).toHaveLength(2000)
    expect(out[0]!.message).toBe('m500')
    expect(out[1999]!.message).toBe('m2499')
  })
  it('passes through when under the cap', () => {
    const lines = [line('a'), line('b')]
    expect(clampBuffer(lines)).toBe(lines)
  })
})

describe('matchesFilter / filterLines (logs.js:296-300,237)', () => {
  const lines: LogLine[] = [
    line('login ok', 'INFO'),
    line('DISK warning', 'WARN'),
    line('boom', 'ERROR'),
    line('trace noise', 'TRACE'),
  ]
  it('keeps only active levels', () => {
    expect(matchesFilter(lines[1]!, new Set(['WARN']), '')).toBe(true)
    expect(matchesFilter(lines[1]!, new Set(['ERROR']), '')).toBe(false)
  })
  it('applies a case-insensitive message substring search', () => {
    expect(matchesFilter(lines[1]!, new Set(['WARN']), 'disk')).toBe(true)
    expect(matchesFilter(lines[1]!, new Set(['WARN']), 'nope')).toBe(false)
  })
  it('filters by level set and search together', () => {
    const out = filterLines(lines, new Set(['INFO', 'WARN', 'ERROR']), 'o')
    expect(out.map((l) => l.message)).toEqual(['login ok', 'boom'])
  })
  it('an empty search matches everything in the active levels', () => {
    const out = filterLines(lines, new Set(['INFO', 'WARN']), '')
    expect(out).toHaveLength(2)
  })
})

describe('countByLevel / visibleCount (logs.js:229-260)', () => {
  const lines: LogLine[] = [
    line('a', 'ERROR'),
    line('b', 'ERROR'),
    line('c', 'WARN'),
    line('d', 'INFO'),
    line('e', 'DEBUG'),
    line('f', 'TRACE'),
  ]
  it('counts errors/warns/infos and folds TRACE into debug', () => {
    expect(countByLevel(lines)).toEqual({
      total: 6,
      errors: 2,
      warns: 1,
      infos: 1,
      debug: 2,
    })
  })
  it('visibleCount matches the filtered length', () => {
    expect(visibleCount(lines, new Set(['ERROR']), '')).toBe(2)
    expect(visibleCount(lines, new Set(['ERROR']), 'a')).toBe(1)
  })
})

describe('sliceTs (logs.js:313)', () => {
  it('slices a timestamp to 23 chars', () => {
    expect(sliceTs('2026-01-01T00:00:00.123456Z')).toBe('2026-01-01T00:00:00.123')
  })
  it('returns "" for a null/undefined ts', () => {
    expect(sliceTs(undefined)).toBe('')
    expect(sliceTs(null)).toBe('')
  })
  it('stringifies a numeric ts before slicing', () => {
    expect(sliceTs(1234567890)).toBe('1234567890')
  })
})

describe('splitHighlight (logs.js:325-330)', () => {
  it('returns a single text segment when the term is empty', () => {
    expect(splitHighlight('hello world', '')).toEqual([{ text: 'hello world', match: false }])
  })
  it('segments matches (case-insensitive) around plain text', () => {
    expect(splitHighlight('Error and error', 'error')).toEqual([
      { text: 'Error', match: true },
      { text: ' and ', match: false },
      { text: 'error', match: true },
    ])
  })
  it('escapes regex metacharacters in the term', () => {
    expect(splitHighlight('a.b.c', '.')).toEqual([
      { text: 'a', match: false },
      { text: '.', match: true },
      { text: 'b', match: false },
      { text: '.', match: true },
      { text: 'c', match: false },
    ])
  })
  it('returns the whole string as text when there is no match', () => {
    expect(splitHighlight('nothing', 'zzz')).toEqual([{ text: 'nothing', match: false }])
  })
})

describe('buildExportText (logs.js:337-346)', () => {
  it('prefixes the sliced ts when present and formats [LEVEL] message', () => {
    const lines: LogLine[] = [
      { level: 'ERROR', message: 'boom', ts: '2026-01-01T00:00:00.000000Z', raw: '' },
      { level: 'INFO', message: 'ok', raw: '' },
    ]
    expect(buildExportText(lines)).toBe('2026-01-01T00:00:00.000 [ERROR] boom\n[INFO] ok')
  })
  it('is empty for no lines', () => {
    expect(buildExportText([])).toBe('')
  })
})

function line(message: string, level: LogLine['level'] = 'INFO'): LogLine {
  return { level, message, raw: message }
}
