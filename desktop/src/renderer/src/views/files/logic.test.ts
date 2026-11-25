import { describe, expect, it } from 'vitest'
import {
  ancestors,
  baseName,
  blockedReason,
  failureKind,
  filterEntries,
  formatBytes,
  normalizeEntry,
  normalizeListing,
  normalizeRead,
  normalizeTicket,
  parentPath,
  type FileEntry,
} from './logic'
import { fill } from './text'

const file = (path: string, extra: Partial<FileEntry> = {}): FileEntry => ({
  name: baseName(path),
  path,
  type: 'file',
  kind: 'text',
  openable: true,
  ...extra,
})

describe('paths', () => {
  it('splits a path', () => {
    expect(baseName('a/b/c.md')).toBe('c.md')
    expect(baseName('c.md')).toBe('c.md')
    expect(parentPath('a/b/c.md')).toBe('a/b')
    expect(parentPath('c.md')).toBe('')
    expect(ancestors('a/b/c.md')).toEqual(['a', 'a/b'])
    expect(ancestors('c.md')).toEqual([])
  })
})

describe('normalizeEntry', () => {
  it('keeps a well-formed entry', () => {
    const entry = normalizeEntry(
      { name: 'a.md', path: 'docs/a.md', type: 'file', kind: 'markdown', size: 3, openable: true },
      'docs',
    )
    expect(entry).toMatchObject({ path: 'docs/a.md', kind: 'markdown', size: 3, openable: true })
  })

  it('drops an entry whose path is not its name under the listed folder', () => {
    const raw = { name: 'a.md', type: 'file', openable: true }
    expect(normalizeEntry({ ...raw, path: '../a.md' }, 'docs')).toBeNull()
    expect(normalizeEntry({ ...raw, path: '/etc/a.md' }, 'docs')).toBeNull()
    expect(normalizeEntry({ ...raw, name: 'x/a.md', path: 'docs/x/a.md' }, 'docs')).toBeNull()
    expect(normalizeEntry({ ...raw, name: '', path: 'docs/' }, 'docs')).toBeNull()
    expect(normalizeEntry({ ...raw, path: 'docs/a.md', type: 'device' }, 'docs')).toBeNull()
    expect(normalizeEntry(null, 'docs')).toBeNull()
  })

  it('is not openable unless the gateway says so', () => {
    const entry = normalizeEntry({ name: 'a', path: 'a', type: 'symlink' }, '')
    expect(entry?.openable).toBe(false)
  })
})

describe('normalizeListing', () => {
  it('tolerates a malformed answer', () => {
    expect(normalizeListing(null, '')).toEqual({
      path: '',
      entries: [],
      truncated: false,
      total: 0,
      exists: true,
    })
    const listing = normalizeListing(
      { entries: [{ name: 'a', path: 'a', type: 'directory', openable: true }, 7], total: 9 },
      '',
    )
    expect(listing.entries).toHaveLength(1)
    expect(listing.total).toBe(9)
  })
})

describe('normalizeRead', () => {
  it('never treats a missing flag as previewable', () => {
    const read = normalizeRead({ content: 'x', kind: 'nonsense' }, 'a/b.txt')
    expect(read).toMatchObject({ previewable: false, kind: 'other', name: 'b.txt', content: 'x' })
  })
})

describe('normalizeTicket', () => {
  it('accepts only the gateway ticket route', () => {
    const ok = normalizeTicket({ url: '/api/v1/artifacts/download/conn-1/abc_DEF-123', size: 4 })
    expect(ok?.url).toBe('/api/v1/artifacts/download/conn-1/abc_DEF-123')
    for (const url of [
      'https://evil.example/x.png',
      '//evil.example/api/v1/artifacts/download/c/t',
      '/api/v1/artifacts/download/c/t?next=https://evil.example',
      '/api/v1/artifacts/download/c/../../secrets',
      'javascript:alert(1)',
      '',
    ]) {
      expect(normalizeTicket({ url })).toBeNull()
    }
  })
})

describe('filterEntries', () => {
  const dir = (path: string): FileEntry => ({
    name: baseName(path),
    path,
    type: 'directory',
    openable: true,
  })
  const entries = [dir('docs'), dir('memory'), file('MEMORY.md'), file('notes.txt')]

  it('returns everything without a query', () => {
    expect(filterEntries(entries, '  ', new Set())).toHaveLength(4)
  })
  it('matches names case-insensitively', () => {
    expect(filterEntries(entries, 'mem', new Set()).map((e) => e.path)).toEqual([
      'memory',
      'MEMORY.md',
    ])
  })
  it('keeps an open folder so matches inside it stay reachable', () => {
    expect(filterEntries(entries, 'notes', new Set(['docs'])).map((e) => e.path)).toEqual([
      'docs',
      'notes.txt',
    ])
  })
})

describe('formatBytes', () => {
  it('formats in binary steps', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1023)).toBe('1023 B')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(20 * 1024 * 1024)).toBe('20 MB')
    expect(formatBytes(undefined)).toBe('')
    expect(formatBytes(-1)).toBe('')
  })
})

describe('failureKind', () => {
  it('reads the gateway code', () => {
    expect(failureKind({ code: 'METHOD_NOT_FOUND' })).toBe('outdated')
    expect(failureKind({ details: { code: 'workspace.denied' } })).toBe('denied')
    expect(failureKind({ details: { code: 'workspace.unsafe' } })).toBe('unsafe')
    expect(failureKind({ details: { code: 'workspace.not_found' } })).toBe('not_found')
    expect(failureKind({ details: { code: 'workspace.too_large' } })).toBe('too_large')
    expect(failureKind({ details: { code: 'workspace.invalid_path' } })).toBe('unsupported')
    expect(failureKind(new Error('socket closed'))).toBe('failed')
    expect(failureKind(null)).toBe('failed')
  })
})

describe('blockedReason', () => {
  it('explains why a row cannot be opened', () => {
    expect(blockedReason(file('a.txt'))).toBeNull()
    expect(blockedReason(file('a', { type: 'symlink', openable: false }))).toBe(
      'files.blocked.symlink',
    )
    expect(blockedReason(file('a', { openable: false, reason: 'hardlink' }))).toBe(
      'files.blocked.hardlink',
    )
    expect(blockedReason(file('a', { openable: false }))).toBe('files.blocked.other')
  })
})

describe('fill', () => {
  it('fills known placeholders and leaves the rest', () => {
    expect(fill('{a} of {b} {c}', { a: 1, b: 'two' })).toBe('1 of two {c}')
  })
})
