// Pure-helper parity tests for the artifact renderer (chat.js:7480-7593).
//
// These are the sanctioned unit-test surface for Task 5: the pure classification
// / URL helpers, verified against the legacy source. The imperative artifact-card
// DOM (appendArtifact / renderArtifacts / renderStreamArtifacts / downloadArtifact)
// is verified by a live-browser sweep (parity matrix), NOT here — it needs the
// live streaming controller + a real gateway serving the download.
//
// The one exception is the chart placeholder, which has no legacy counterpart:
// it renders no visible card of its own, so a broken placeholder shows the user
// nothing at all rather than a wrong-looking chip. Its hooks and its handoff to
// the chart mounter are pinned below.

import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import {
  artifactMime,
  artifactName,
  artifactExtension,
  artifactCategory,
  artifactCategoryLabel,
  createArtifactRenderer,
  isImageArtifact,
  isAudioArtifact,
  artifactDownloadUrl,
  artifactPreviewUrl,
  artifactAuthenticatedDownloadUrl,
  type Artifact,
  type ArtifactRendererDeps,
} from './artifacts'
import { CHART_ARTIFACT_MIME } from './chart'

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
  it('classifies the AgentOS chart mime as "chart" (no legacy counterpart)', () => {
    // Wins over the +json extension fallback, which would otherwise land the
    // chart payload in 'data' and render it as a download chip.
    expect(artifactCategory({ mime: CHART_ARTIFACT_MIME, name: 'bonk.chart.json' } as never)).toBe(
      'chart',
    )
    expect(artifactCategory({ mime: 'application/json', name: 'x.json' } as never)).toBe('data')
  })
})

/* ── artifactCategoryLabel (chat.js:7551) ───────────────────────────────── */

describe('artifactCategoryLabel (parity chat.js:7551)', () => {
  it('maps category → chip label', () => {
    expect(artifactCategoryLabel('data')).toBe('data')
    expect(artifactCategoryLabel('document')).toBe('doc')
    expect(artifactCategoryLabel('code')).toBe('code')
    expect(artifactCategoryLabel('audio')).toBe('audio')
    expect(artifactCategoryLabel('chart')).toBe('chart')
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

/* ── chart placeholder + mounter handoff (AgentOS-native) ───────────────── */

const CHART_ARTIFACT: Artifact = {
  id: 'art-1',
  name: 'bonk.chart.json',
  mime: CHART_ARTIFACT_MIME,
  download_url: '/api/v1/artifacts/art-1',
}

function chartRendererDeps(overrides: Partial<ArtifactRendererDeps> = {}) {
  const bubble = document.createElement('div')
  const body = document.createElement('div')
  body.className = 'msg-body'
  bubble.appendChild(body)
  const streamArtifacts: Artifact[] = []
  const deps: ArtifactRendererDeps = {
    ensureStreamBubble: () => bubble,
    markVisibleStreamEvent: () => {},
    scrollToBottom: () => {},
    getAutoScroll: () => false,
    getStreamBubble: () => bubble,
    pushStreamArtifact: (artifact) => streamArtifacts.push(artifact),
    getStreamArtifacts: () => streamArtifacts,
    getSessionKey: () => 'agent:main:webchat:test',
    getAuthToken: () => 'tok',
    esc: (value) => value,
    ...overrides,
  }
  return { deps, body, streamArtifacts }
}

describe('createArtifactRenderer chart artifacts', () => {
  it('renders a mount placeholder carrying the hooks the mounter looks for', () => {
    const { deps } = chartRendererDeps()

    const container = document.createElement('div')
    container.innerHTML = createArtifactRenderer(deps).renderArtifacts([CHART_ARTIFACT])

    const host = container.querySelector<HTMLElement>('[data-chart-src]')
    expect(host).not.toBeNull()
    // The payload URL must be authenticated the same way a download is.
    expect(host?.dataset.chartSrc).toBe(
      '/api/v1/artifacts/art-1?sessionKey=agent%3Amain%3Awebchat%3Atest&token=tok',
    )
    expect(host?.querySelector('.msg-artifact-chart__canvas')).not.toBeNull()
    expect(host?.querySelector('.msg-artifact-chart__status')).not.toBeNull()
    // The mounter fills this on draw; it must exist for the crosshair readout.
    expect(host?.querySelector('.msg-artifact-chart__readout')).not.toBeNull()
    expect(host?.querySelector('.msg-artifact-chart__name')).toHaveTextContent('bonk.chart.json')
    // A chart groups with charts, never into the file-chip row.
    expect(container.querySelector('.msg-artifact-charts')).not.toBeNull()
    expect(container.querySelector('.msg-artifact-files')).toBeNull()
  })

  it('does not turn a click on the chart itself into a download', () => {
    // useTranscript delegates clicks: any non-anchor element that resolves to
    // [data-artifact-download] downloads the file. A chart is interactive, so
    // the host must not carry it — otherwise every pan, zoom and crosshair
    // click fetches the JSON instead of moving the chart.
    const { deps } = chartRendererDeps()

    const container = document.createElement('div')
    container.innerHTML = createArtifactRenderer(deps).renderArtifacts([CHART_ARTIFACT])

    const canvas = container.querySelector<HTMLElement>('.msg-artifact-chart__canvas')
    expect(canvas?.closest('[data-artifact-download]')).toBeNull()
    expect(container.querySelector('.msg-artifact-chart')).not.toHaveAttribute(
      'data-artifact-download',
    )
  })

  it('renders a download button rather than a raw-json anchor', () => {
    const { deps } = chartRendererDeps()

    const container = document.createElement('div')
    container.innerHTML = createArtifactRenderer(deps).renderArtifacts([CHART_ARTIFACT])

    const button = container.querySelector<HTMLElement>('.msg-artifact-chart__download')
    expect(button?.tagName).toBe('BUTTON')
    expect(button?.getAttribute('type')).toBe('button')
    // No data-artifact-download, so the delegated handler does not fetch JSON.
    expect(button?.hasAttribute('data-artifact-download')).toBe(false)
    expect(button?.textContent).toBe('Download')
  })

  it('hands a streamed chart artifact to the mounter as soon as it lands', () => {
    const mountCharts = vi.fn()
    const { deps, body } = chartRendererDeps({ mountCharts })

    createArtifactRenderer(deps).appendArtifact(CHART_ARTIFACT)

    // Without this call the placeholder sits at "Loading chart…" forever.
    expect(mountCharts).toHaveBeenCalledWith(body)
    expect(body.querySelector('[data-chart-src]')).not.toBeNull()
  })

  it('hands flushed stream artifacts to the mounter on the settle pass', () => {
    const mountCharts = vi.fn()
    const { deps, body, streamArtifacts } = chartRendererDeps({ mountCharts })
    streamArtifacts.push(CHART_ARTIFACT)

    createArtifactRenderer(deps).renderStreamArtifacts()

    expect(mountCharts).toHaveBeenCalledWith(body)
  })

  it('renders inert cards when no mounter is composed in', () => {
    const { deps, body } = chartRendererDeps()

    expect(() => createArtifactRenderer(deps).appendArtifact(CHART_ARTIFACT)).not.toThrow()
    expect(body.querySelector('[data-chart-src]')).not.toBeNull()
  })
})

/* ── off-gateway hosts (the desktop renderer) ───────────────────────────── */

describe('artifact URLs on an off-gateway host', () => {
  // The desktop renderer is loaded from disk and points the shared URL helpers
  // at the gateway it manages (lib/api-origin.ts). A relative `/api/...` there
  // resolves against `file://` (or the dev server), never against the gateway,
  // so the chip's href, the image preview, the audio source and the chart
  // payload all fail silently. Off gateway every artifact URL keeps the
  // gateway origin; the console (served by the gateway) stays relative.
  const ORIGIN = 'http://127.0.0.1:18791'
  const SESSION = 'agent%3Amain%3Awebchat%3Atest'
  const ctx = { sessionKey: 'agent:main:webchat:test', token: 'tok' }

  beforeEach(() => {
    window.__AGENTOS_ENV__ = { apiOrigin: `${ORIGIN}/`, controlBase: '/control' }
  })
  afterEach(() => {
    delete window.__AGENTOS_ENV__
  })

  it('resolves a relative download_url (or the id fallback) against the gateway origin', () => {
    expect(artifactDownloadUrl({ download_url: '/api/v1/artifacts/42?sessionKey=s' })).toBe(
      `${ORIGIN}/api/v1/artifacts/42`,
    )
    expect(artifactDownloadUrl({ id: 'abc 1' })).toBe(`${ORIGIN}/api/v1/artifacts/abc%201`)
  })

  it('keeps an already-absolute download_url on its own origin', () => {
    expect(artifactDownloadUrl({ download_url: 'https://files.example/x.png?sessionKey=s' })).toBe(
      'https://files.example/x.png',
    )
  })

  it('authenticates preview and download URLs on the gateway origin', () => {
    expect(artifactPreviewUrl({ download_url: '/api/v1/artifacts/7' }, ctx)).toBe(
      `${ORIGIN}/api/v1/artifacts/7?sessionKey=${SESSION}&token=tok`,
    )
    expect(artifactAuthenticatedDownloadUrl('/api/v1/artifacts/5', ctx)).toBe(
      `${ORIGIN}/api/v1/artifacts/5?sessionKey=${SESSION}&token=tok`,
    )
  })

  it('renders every card URL against the gateway origin', () => {
    const { deps } = chartRendererDeps()
    const container = document.createElement('div')
    container.innerHTML = createArtifactRenderer(deps).renderArtifacts([
      {
        id: 'x-1',
        name: 'report.xlsx',
        mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        size: 2048,
        download_url: '/api/v1/artifacts/x-1',
      },
      { id: 'i-1', name: 'plot.png', mime: 'image/png', download_url: '/api/v1/artifacts/i-1' },
      { id: 'a-1', name: 'clip.wav', mime: 'audio/wav', download_url: '/api/v1/artifacts/a-1' },
      CHART_ARTIFACT,
    ])

    const chip = container.querySelector<HTMLAnchorElement>('.msg-artifact-chip')
    expect(chip?.getAttribute('href')).toBe(
      `${ORIGIN}/api/v1/artifacts/x-1?sessionKey=${SESSION}&token=tok`,
    )
    expect(chip?.dataset.artifactDownload).toBe(`${ORIGIN}/api/v1/artifacts/x-1`)
    expect(container.querySelector('.msg-artifact-preview')?.getAttribute('src')).toBe(
      `${ORIGIN}/api/v1/artifacts/i-1?sessionKey=${SESSION}&token=tok`,
    )
    expect(container.querySelector('.msg-artifact-audio')?.getAttribute('src')).toBe(
      `${ORIGIN}/api/v1/artifacts/a-1?sessionKey=${SESSION}&token=tok`,
    )
    expect(container.querySelector<HTMLElement>('[data-chart-src]')?.dataset.chartSrc).toBe(
      `${ORIGIN}/api/v1/artifacts/art-1?sessionKey=${SESSION}&token=tok`,
    )
  })
})
