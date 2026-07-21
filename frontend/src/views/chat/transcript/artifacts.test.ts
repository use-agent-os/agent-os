// Pure-helper parity tests for the artifact renderer (chat.js:7480-7593).
//
// These are the sanctioned unit-test surface for Task 5: the pure classification
// / URL helpers, verified against the legacy source. The imperative artifact-card
// DOM (appendArtifact / renderArtifacts / renderStreamArtifacts / downloadArtifact)
// is verified by a live-browser sweep (parity matrix), NOT here — it needs the
// live streaming controller + a real gateway serving the download.

import { describe, it, expect } from 'vitest'
import {
  artifactMime,
  artifactName,
  artifactExtension,
  artifactCategory,
  artifactCategoryLabel,
  isImageArtifact,
  isAudioArtifact,
  artifactDownloadUrl,
  artifactPreviewUrl,
  artifactAuthenticatedDownloadUrl,
} from './artifacts'

/* ── artifactMime / artifactName (chat.js:7523-7529) ────────────────────── */

describe('artifactMime (parity chat.js:7523)', () => {
  it('lowercases a present mime', () => {
    expect(artifactMime({ mime: 'Image/PNG' } as never)).toBe('image/png')
  })
  it('returns "" when mime is absent', () => {
    expect(artifactMime({} as never)).toBe('')
    expect(artifactMime(null as never)).toBe('')
  })
})

describe('artifactName (parity chat.js:7527)', () => {
  it('returns the name when present', () => {
    expect(artifactName({ name: 'report.md' } as never)).toBe('report.md')
  })
  it('falls back to "artifact" when absent', () => {
    expect(artifactName({} as never)).toBe('artifact')
    expect(artifactName(null as never)).toBe('artifact')
  })
})

/* ── artifactExtension (chat.js:7531) ───────────────────────────────────── */

describe('artifactExtension (parity chat.js:7531)', () => {
  it('derives a lowercased extension from a name', () => {
    expect(artifactExtension('report.md')).toBe('md')
    expect(artifactExtension('DATA.CSV')).toBe('csv')
  })
  it('returns "" when there is no dot', () => {
    expect(artifactExtension('README')).toBe('')
  })
  it('returns "" for a trailing dot (idx === length-1)', () => {
    expect(artifactExtension('name.')).toBe('')
  })
  it('tolerates empty / nullish input', () => {
    expect(artifactExtension('')).toBe('')
    expect(artifactExtension(undefined as never)).toBe('')
  })
})

/* ── artifactCategory (chat.js:7538) ────────────────────────────────────── */

describe('artifactCategory (parity chat.js:7538)', () => {
  it('classifies an image/* mime as "visual" (NOT "image")', () => {
    // Legacy chat.js:7540 returns 'visual', not the brief-example 'image'.
    expect(artifactCategory({ mime: 'image/png', name: 'x.png' } as never)).toBe('visual')
  })
  it('classifies an audio/* mime as "audio"', () => {
    expect(artifactCategory({ mime: 'audio/mpeg', name: 'x.mp3' } as never)).toBe('audio')
  })
  it('maps a known mime via ARTIFACT_MIME_CATEGORIES', () => {
    expect(artifactCategory({ mime: 'application/json' } as never)).toBe('data')
    expect(artifactCategory({ mime: 'text/markdown' } as never)).toBe('document')
    expect(artifactCategory({ mime: 'text/csv' } as never)).toBe('data')
  })
  it('falls back to extension when mime is empty / octet-stream / "artifact"', () => {
    expect(artifactCategory({ name: 'q.sql' } as never)).toBe('code')
    expect(artifactCategory({ mime: 'application/octet-stream', name: 'a.json' } as never)).toBe(
      'data',
    )
    expect(artifactCategory({ mime: 'artifact', name: 'notes.md' } as never)).toBe('document')
  })
  it('classifies audio extensions when mime is unknown', () => {
    expect(artifactCategory({ name: 'song.flac' } as never)).toBe('audio')
    expect(artifactCategory({ mime: 'artifact', name: 'clip.webm' } as never)).toBe('audio')
  })
  it('returns "file" for an unknown mime + unknown extension', () => {
    expect(artifactCategory({ mime: 'application/x-thing', name: 'blob.xyz' } as never)).toBe(
      'file',
    )
    expect(artifactCategory({} as never)).toBe('file')
  })
})

/* ── artifactCategoryLabel (chat.js:7551) ───────────────────────────────── */

describe('artifactCategoryLabel (parity chat.js:7551)', () => {
  it('maps category → chip label', () => {
    expect(artifactCategoryLabel('data')).toBe('data')
    expect(artifactCategoryLabel('document')).toBe('doc')
    expect(artifactCategoryLabel('code')).toBe('code')
    expect(artifactCategoryLabel('audio')).toBe('audio')
  })
  it('defaults unknown / visual / file categories to "file"', () => {
    expect(artifactCategoryLabel('visual')).toBe('file')
    expect(artifactCategoryLabel('file')).toBe('file')
    expect(artifactCategoryLabel('whatever')).toBe('file')
  })
})

/* ── isImageArtifact / isAudioArtifact (chat.js:7561/7565) ──────────────── */

describe('isImageArtifact / isAudioArtifact (parity chat.js:7561/7565)', () => {
  it('isImageArtifact is true only for the "visual" category', () => {
    expect(isImageArtifact({ mime: 'image/gif', name: 'x.gif' } as never)).toBe(true)
    expect(isImageArtifact({ mime: 'audio/wav', name: 'x.wav' } as never)).toBe(false)
    expect(isImageArtifact({ mime: 'text/markdown' } as never)).toBe(false)
  })
  it('isAudioArtifact is true only for the "audio" category', () => {
    expect(isAudioArtifact({ mime: 'audio/mpeg', name: 'x.mp3' } as never)).toBe(true)
    expect(isAudioArtifact({ name: 'clip.opus' } as never)).toBe(true)
    expect(isAudioArtifact({ mime: 'image/png', name: 'x.png' } as never)).toBe(false)
  })
})

/* ── artifactDownloadUrl (chat.js:7480) ─────────────────────────────────── */

describe('artifactDownloadUrl (parity chat.js:7480)', () => {
  it('uses download_url verbatim (relative path)', () => {
    expect(artifactDownloadUrl({ download_url: '/api/v1/artifacts/42' } as never)).toBe(
      '/api/v1/artifacts/42',
    )
  })
  it('falls back to /api/v1/artifacts/<id> when no download_url', () => {
    expect(artifactDownloadUrl({ id: 'abc 1' } as never)).toBe('/api/v1/artifacts/abc%201')
  })
  it('returns "" when neither download_url nor id is present', () => {
    expect(artifactDownloadUrl({} as never)).toBe('')
    expect(artifactDownloadUrl(null as never)).toBe('')
  })
  it('strips sessionKey / session_key query params', () => {
    expect(
      artifactDownloadUrl({
        download_url: '/api/v1/artifacts/9?sessionKey=s1&session_key=s2&keep=1',
      } as never),
    ).toBe('/api/v1/artifacts/9?keep=1')
  })
})

/* ── artifactPreviewUrl (chat.js:7569) ──────────────────────────────────── */

describe('artifactPreviewUrl (parity chat.js:7569)', () => {
  it('adds sessionKey + token onto the download url', () => {
    const url = artifactPreviewUrl({ download_url: '/api/v1/artifacts/7' } as never, {
      sessionKey: 'sess-1',
      token: 'tok-9',
    })
    expect(url).toBe('/api/v1/artifacts/7?sessionKey=sess-1&token=tok-9')
  })
  it('omits token when none, omits sessionKey when none', () => {
    expect(
      artifactPreviewUrl({ download_url: '/api/v1/artifacts/7' } as never, {
        sessionKey: '',
        token: '',
      }),
    ).toBe('/api/v1/artifacts/7')
  })
  it('returns "" when there is no download url', () => {
    expect(artifactPreviewUrl({} as never, { sessionKey: 's', token: 't' })).toBe('')
  })
})

/* ── artifactAuthenticatedDownloadUrl (chat.js:7583) ────────────────────── */

describe('artifactAuthenticatedDownloadUrl (parity chat.js:7583)', () => {
  it('adds sessionKey + token onto a raw url', () => {
    expect(
      artifactAuthenticatedDownloadUrl('/api/v1/artifacts/5', {
        sessionKey: 'k',
        token: 'z',
      }),
    ).toBe('/api/v1/artifacts/5?sessionKey=k&token=z')
  })
  it('returns "" for an empty raw url', () => {
    expect(artifactAuthenticatedDownloadUrl('', { sessionKey: 'k', token: 'z' })).toBe('')
  })
  it('omits token when absent', () => {
    expect(
      artifactAuthenticatedDownloadUrl('/api/v1/artifacts/5', { sessionKey: 'k', token: '' }),
    ).toBe('/api/v1/artifacts/5?sessionKey=k')
  })
})
