import { describe, expect, it } from 'vitest'
import {
  ancestors,
  failureKind,
  filterEntries,
  formatBytes,
  normalizeEntry,
  normalizeListing,
  normalizeRead,
  normalizeTicket,
  parentPath,
  type FileEntry,
} from './workspace-files'

describe('workspace files on the wire', () => {
  it('keeps an entry only when its path is its name under the folder', () => {
    expect(normalizeEntry({ name: 'a.md', path: 'docs/a.md', type: 'file' }, 'docs')).toMatchObject(
      {
        path: 'docs/a.md',
        openable: false,
      },
    )
    expect(normalizeEntry({ name: 'a.md', path: '../a.md', type: 'file' }, 'docs')).toBeNull()
    expect(normalizeEntry({ name: 'x/y', path: 'x/y', type: 'file' }, '')).toBeNull()
    expect(normalizeEntry({ name: 'a', path: 'a', type: 'device' }, '')).toBeNull()
    expect(normalizeEntry(null, '')).toBeNull()
  })

  it('normalizes a listing and a read', () => {
    const listing = normalizeListing(
      { entries: [{ name: 'a', path: 'a', type: 'directory', openable: true }, 7], exists: false },
      '',
    )
    expect(listing.entries).toHaveLength(1)
    expect(listing.exists).toBe(false)
    const read = normalizeRead({ kind: 'weird', size: -3, content: 5 }, 'docs/a.bin')
    expect(read).toMatchObject({ name: 'a.bin', kind: 'other', size: 0, content: '' })
  })

  it('accepts only the gateway ticket route as a download URL', () => {
    expect(normalizeTicket({ url: '/api/v1/artifacts/download/conn-1/abc_DEF-1' })?.url).toBe(
      '/api/v1/artifacts/download/conn-1/abc_DEF-1',
    )
    expect(normalizeTicket({ url: 'https://evil.test/x' })).toBeNull()
    expect(normalizeTicket({ url: '/api/v1/artifacts/download/c/../../x' })).toBeNull()
    expect(normalizeTicket({ url: 'javascript:alert(1)' })).toBeNull()
  })

  it('maps refusals to what the UI says', () => {
    expect(failureKind({ code: 'METHOD_NOT_FOUND' })).toBe('outdated')
    expect(failureKind({ details: { code: 'workspace.denied' } })).toBe('denied')
    expect(failureKind({ details: { code: 'workspace.not_a_file' } })).toBe('not_found')
    expect(failureKind(new Error('boom'))).toBe('failed')
  })
})

describe('workspace file helpers', () => {
  it('walks paths', () => {
    expect(parentPath('a/b/c.md')).toBe('a/b')
    expect(parentPath('c.md')).toBe('')
    expect(ancestors('a/b/c.md')).toEqual(['a', 'a/b'])
  })

  it('filters names and keeps open folders', () => {
    const entries = [
      { name: 'docs', path: 'docs', type: 'directory', openable: true },
      { name: 'src', path: 'src', type: 'directory', openable: true },
      { name: 'Report.md', path: 'Report.md', type: 'file', openable: true },
    ] as FileEntry[]
    expect(filterEntries(entries, 'rep', new Set(['src'])).map((e) => e.name)).toEqual([
      'src',
      'Report.md',
    ])
    expect(filterEntries(entries, '  ', new Set())).toHaveLength(3)
  })

  it('formats sizes', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(20 * 1024 * 1024)).toBe('20 MB')
    expect(formatBytes(undefined)).toBe('')
  })
})
