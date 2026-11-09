// Pure-helper parity tests for the artifact renderer.
// These are the sanctioned unit-test surface for Task 5: the pure classification
// / URL helpers, verified against the legacy source. The imperative artifact-card
// DOM (appendArtifact / renderArtifacts / renderStreamArtifacts / downloadArtifact)
// is verified by a live-browser sweep (parity matrix), NOT here — it needs the
// live streaming controller + a real gateway serving the download.
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
  artifactKind,
  artifactSizeLabel,
  artifactSummary,
  artifactHasThumbnail,
  type Artifact,
  type ArtifactRendererDeps,
} from './artifacts'
import { CHART_ARTIFACT_MIME } from './chart'
import { DCA_ARTIFACT_MIME } from './dca'
import { MARKETS_ARTIFACT_MIME } from './markets'
import { TRIGGER_ARTIFACT_MIME } from './trigger'
import { LP_ARTIFACT_MIME } from './lp'

/* ── artifactMime / artifactName ────────────────────── */

describe('artifactMime', () => {
  it('lowercases a present mime', () => {
    expect(artifactMime({ mime: 'Image/PNG' } as never)).toBe('image/png')
  })
  it('returns "" when mime is absent', () => {
    expect(artifactMime({} as never)).toBe('')
    expect(artifactMime(null as never)).toBe('')
  })
})

describe('artifactName', () => {
  it('returns the name when present', () => {
    expect(artifactName({ name: 'report.md' } as never)).toBe('report.md')
  })
  it('falls back to "artifact" when absent', () => {
    expect(artifactName({} as never)).toBe('artifact')
    expect(artifactName(null as never)).toBe('artifact')
  })
})

/* ── artifactExtension ───────────────────────────────────── */

describe('artifactExtension', () => {
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

/* ── artifactCategory ────────────────────────────────────── */

describe('artifactCategory', () => {
  it('classifies an image/* mime as "visual" (NOT "image")', () => {
    // returns 'visual', not the brief-example 'image'.
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
  it('classifies the AgentOS LP mime as "lp" (no legacy counterpart)', () => {
    expect(artifactCategory({ mime: LP_ARTIFACT_MIME, name: 'pepe.lp.json' } as never)).toBe('lp')
    expect(
      artifactCategory({ mime: `${LP_ARTIFACT_MIME}; charset=utf-8`, name: 'x.json' } as never),
    ).toBe('lp')
  })
  it('classifies the AgentOS DCA mime as "dca" (no legacy counterpart)', () => {
    expect(artifactCategory({ mime: DCA_ARTIFACT_MIME, name: 'mandate.json' } as never)).toBe('dca')
    expect(
      artifactCategory({ mime: `${DCA_ARTIFACT_MIME}; charset=utf-8`, name: 'x.json' } as never),
    ).toBe('dca')
  })
  it('classifies the AgentOS trigger mime as "trigger" (no legacy counterpart)', () => {
    expect(artifactCategory({ mime: TRIGGER_ARTIFACT_MIME, name: 'trigger.json' } as never)).toBe(
      'trigger',
    )
    expect(
      artifactCategory({
        mime: `${TRIGGER_ARTIFACT_MIME}; charset=utf-8`,
        name: 'x.json',
      } as never),
    ).toBe('trigger')
    expect(
      artifactCategory({ mime: 'APPLICATION/VND.AGENTOS.TRIGGER+JSON', name: 'x.json' } as never),
    ).toBe('trigger')
    // A near miss is not a trigger card.
    expect(
      artifactCategory({ mime: 'application/vnd.agentos.triggers+json', name: 'x.json' } as never),
    ).not.toBe('trigger')
  })
})

/* ── artifactCategoryLabel ───────────────────────────────── */

describe('artifactCategoryLabel', () => {
  it('maps category → chip label', () => {
    expect(artifactCategoryLabel('data')).toBe('data')
    expect(artifactCategoryLabel('document')).toBe('doc')
    expect(artifactCategoryLabel('code')).toBe('code')
    expect(artifactCategoryLabel('audio')).toBe('audio')
    expect(artifactCategoryLabel('chart')).toBe('chart')
    expect(artifactCategoryLabel('lp')).toBe('lp')
    expect(artifactCategoryLabel('dca')).toBe('dca')
    expect(artifactCategoryLabel('trigger')).toBe('trigger')
    expect(artifactCategoryLabel('markets')).toBe('markets')
  })
  it('defaults unknown / visual / file categories to "file"', () => {
    expect(artifactCategoryLabel('visual')).toBe('file')
    expect(artifactCategoryLabel('file')).toBe('file')
    expect(artifactCategoryLabel('whatever')).toBe('file')
  })
})

/* ── isImageArtifact / isAudioArtifact ──────────────── */

describe('isImageArtifact / isAudioArtifact', () => {
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

/* ── artifactDownloadUrl ─────────────────────────────────── */

describe('artifactDownloadUrl', () => {
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

/* ── artifactPreviewUrl ──────────────────────────────────── */

describe('artifactPreviewUrl', () => {
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

/* ── artifactAuthenticatedDownloadUrl ────────────────────── */

describe('artifactAuthenticatedDownloadUrl', () => {
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

/* ── LP card placeholder + mounter handoff (AgentOS-native) ─────────────── */

const LP_ARTIFACT: Artifact = {
  id: 'lp-1',
  name: 'pepe.position.json',
  mime: LP_ARTIFACT_MIME,
  download_url: '/api/v1/artifacts/lp-1',
}

describe('createArtifactRenderer LP artifacts', () => {
  it('renders a mount placeholder carrying the hooks the LP mounter looks for', () => {
    const { deps } = chartRendererDeps()
    const container = document.createElement('div')
    container.innerHTML = createArtifactRenderer(deps).renderArtifacts([LP_ARTIFACT])

    const host = container.querySelector<HTMLElement>('[data-lp-src]')
    expect(host).not.toBeNull()
    expect(host?.dataset.lpSrc).toBe(
      '/api/v1/artifacts/lp-1?sessionKey=agent%3Amain%3Awebchat%3Atest&token=tok',
    )
    expect(host?.dataset.artifactCategory).toBe('lp')
    expect(host?.querySelector('.msg-artifact-lp__body')).not.toBeNull()
    expect(host?.querySelector('.msg-artifact-lp__status')).toHaveTextContent('Loading liquidity…')
    // Its own group, never the file-chip row, and never a download target: the
    // card has buttons and links of its own.
    expect(container.querySelector('.msg-artifact-lp-group')).not.toBeNull()
    expect(container.querySelector('.msg-artifact-files')).toBeNull()
    expect(container.querySelector('[data-artifact-download]')).toBeNull()
  })

  it('hands a streamed LP artifact to the mounter as soon as it lands', () => {
    const mountCharts = vi.fn()
    const { deps, body } = chartRendererDeps({ mountCharts })

    createArtifactRenderer(deps).appendArtifact(LP_ARTIFACT)

    expect(mountCharts).toHaveBeenCalledWith(body)
    expect(body.querySelector('[data-lp-src]')).not.toBeNull()
  })
})

/* ── DCA card placeholder + mounter handoff (AgentOS-native) ────────────── */

const DCA_ARTIFACT: Artifact = {
  id: 'dca-1',
  name: 'mandate-eth-20260928T054800Z.json',
  mime: DCA_ARTIFACT_MIME,
  download_url: '/api/v1/artifacts/dca-1',
}

describe('createArtifactRenderer DCA artifacts', () => {
  it('renders a mount placeholder carrying the hooks the DCA mounter looks for', () => {
    const { deps } = chartRendererDeps()
    const container = document.createElement('div')
    container.innerHTML = createArtifactRenderer(deps).renderArtifacts([DCA_ARTIFACT, LP_ARTIFACT])

    const host = container.querySelector<HTMLElement>('[data-dca-src]')
    expect(host).not.toBeNull()
    expect(host?.classList.contains('msg-artifact-dca')).toBe(true)
    expect(host?.dataset.dcaSrc).toBe(
      '/api/v1/artifacts/dca-1?sessionKey=agent%3Amain%3Awebchat%3Atest&token=tok',
    )
    expect(host?.dataset.artifactCategory).toBe('dca')
    expect(host?.querySelector('.msg-artifact-dca__body')).not.toBeNull()
    expect(host?.querySelector('.msg-artifact-dca__status')).toHaveTextContent(
      'Loading DCA mandate…',
    )
    // Its own group, apart from the LP group next to it; never a download target.
    expect(container.querySelector('.msg-artifact-dca-group [data-dca-src]')).toBe(host)
    expect(container.querySelector('.msg-artifact-dca-group [data-lp-src]')).toBeNull()
    expect(container.querySelector('.msg-artifact-files')).toBeNull()
    expect(container.querySelector('[data-artifact-download]')).toBeNull()
  })

  it('hands a streamed DCA artifact to the mounter as soon as it lands', () => {
    const mountCharts = vi.fn()
    const { deps, body } = chartRendererDeps({ mountCharts })

    createArtifactRenderer(deps).appendArtifact(DCA_ARTIFACT)

    expect(mountCharts).toHaveBeenCalledWith(body)
    expect(body.querySelector('[data-dca-src]')).not.toBeNull()
  })
})

/* ── trigger card placeholder + mounter handoff (AgentOS-native) ────────── */

const TRIGGER_ARTIFACT: Artifact = {
  id: 'trg-1',
  name: 'trigger-stop-loss-weth-20261004T091500Z.json',
  mime: TRIGGER_ARTIFACT_MIME,
  download_url: '/api/v1/artifacts/trg-1',
}

describe('createArtifactRenderer trigger artifacts', () => {
  it('renders a mount placeholder carrying the hooks the trigger mounter looks for', () => {
    const { deps } = chartRendererDeps()
    const container = document.createElement('div')
    container.innerHTML = createArtifactRenderer(deps).renderArtifacts([
      TRIGGER_ARTIFACT,
      DCA_ARTIFACT,
    ])

    const host = container.querySelector<HTMLElement>('[data-trigger-src]')
    expect(host).not.toBeNull()
    expect(host?.classList.contains('msg-artifact-trigger')).toBe(true)
    expect(host?.dataset.triggerSrc).toBe(
      '/api/v1/artifacts/trg-1?sessionKey=agent%3Amain%3Awebchat%3Atest&token=tok',
    )
    expect(host?.dataset.artifactCategory).toBe('trigger')
    expect(host?.querySelector('.msg-artifact-trigger__body')).not.toBeNull()
    expect(host?.querySelector('.msg-artifact-trigger__status')).toHaveTextContent(
      'Loading trigger…',
    )
    // Its own group, apart from the DCA group next to it; never a download target.
    expect(container.querySelector('.msg-artifact-trigger-group [data-trigger-src]')).toBe(host)
    expect(container.querySelector('.msg-artifact-trigger-group [data-dca-src]')).toBeNull()
    expect(container.querySelector('.msg-artifact-dca-group [data-dca-src]')).not.toBeNull()
    expect(container.querySelector('.msg-artifact-files')).toBeNull()
    expect(container.querySelector('[data-artifact-download]')).toBeNull()
  })

  it('hands a streamed trigger artifact to the mounter as soon as it lands', () => {
    const mountCharts = vi.fn()
    const { deps, body } = chartRendererDeps({ mountCharts })

    createArtifactRenderer(deps).appendArtifact(TRIGGER_ARTIFACT)

    expect(mountCharts).toHaveBeenCalledWith(body)
    expect(body.querySelector('[data-trigger-src]')).not.toBeNull()
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

/* ── kind + summary (AgentOS-native; feeds the desktop's card skin) ──────── */

describe('artifactKind', () => {
  const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

  it('reads the extension first: the authoring tools name their files precisely', () => {
    expect(artifactKind({ name: 'report.xlsx', mime: 'application/octet-stream' })).toBe(
      'spreadsheet',
    )
    expect(artifactKind({ name: 'prices.csv', mime: 'artifact' })).toBe('spreadsheet')
    expect(artifactKind({ name: 'brief.docx' })).toBe('document')
    expect(artifactKind({ name: 'brief.pdf' })).toBe('pdf')
    expect(artifactKind({ name: 'deck.pptx' })).toBe('presentation')
    expect(artifactKind({ name: 'bundle.zip' })).toBe('archive')
    expect(artifactKind({ name: 'rows.jsonl' })).toBe('data')
    expect(artifactKind({ name: 'NOTES.md' })).toBe('text')
    expect(artifactKind({ name: 'page.html' })).toBe('web')
    expect(artifactKind({ name: 'script.py' })).toBe('code')
  })

  it('falls back to the mime when the name has no telling extension', () => {
    expect(artifactKind({ name: 'export', mime: XLSX })).toBe('spreadsheet')
    expect(artifactKind({ name: 'export', mime: 'application/pdf' })).toBe('pdf')
    expect(artifactKind({ name: 'export', mime: 'text/plain' })).toBe('text')
  })

  it('classifies media by mime family, and everything unknown as a file', () => {
    expect(artifactKind({ name: 'plot.png', mime: 'image/png' })).toBe('image')
    expect(artifactKind({ name: 'clip.wav', mime: 'audio/wav' })).toBe('audio')
    expect(artifactKind({ name: 'clip.mp3' })).toBe('audio')
    expect(artifactKind({ name: 'blob.bin', mime: 'application/octet-stream' })).toBe('file')
    expect(artifactKind(null)).toBe('file')
  })
})

describe('artifactSizeLabel / artifactSummary', () => {
  it('says sizes the way Finder does', () => {
    expect(artifactSizeLabel(5343)).toBe('5 KB')
    expect(artifactSizeLabel(10)).toBe('1 KB')
    expect(artifactSizeLabel(3 * 1024 * 1024 + 200_000)).toBe('3.2 MB')
    expect(artifactSizeLabel(0)).toBe('')
    expect(artifactSizeLabel(undefined)).toBe('')
  })

  it('joins kind, upper-cased extension and size, dropping what is unknown', () => {
    expect(
      artifactSummary({
        name: 'danh-sach-trai-cay.xlsx',
        mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        size: 5343,
      }),
    ).toBe('Spreadsheet · XLSX · 5 KB')
    // The extension only repeats the kind here, so it is left out.
    expect(artifactSummary({ name: 'brief.pdf', size: 212 * 1024 })).toBe('PDF · 212 KB')
    expect(artifactSummary({ name: 'export', mime: 'text/csv', size: 2048 })).toBe(
      'Spreadsheet · 2 KB',
    )
    expect(artifactSummary({})).toBe('File')
  })

  it('stamps kind, summary and the action label on the file chip for a card skin', () => {
    const { deps } = chartRendererDeps()
    const container = document.createElement('div')
    container.innerHTML = createArtifactRenderer(deps).renderArtifacts([
      { id: 'x-1', name: 'report.xlsx', mime: 'application/octet-stream', size: 5343 },
    ])
    const chip = container.querySelector<HTMLElement>('.msg-artifact-chip')
    expect(chip?.dataset.artifactKind).toBe('spreadsheet')
    expect(chip?.dataset.artifactSummary).toBe('Spreadsheet · XLSX · 5 KB')
    expect(chip?.dataset.artifactAction).toBe('Download')
    expect(chip?.getAttribute('title')).toBe('Download report.xlsx')
    // The console's spans are still there for its own skin.
    expect(chip?.querySelector('.msg-file-chip__name')?.textContent).toBe('report.xlsx')
    expect(chip?.querySelector('.msg-file-chip__meta')?.textContent).toBe(
      'application/octet-stream · 5 KB',
    )
  })
})

/* ── markets card placeholder + mounter handoff (AgentOS-native) ────────── */

const MARKETS_ARTIFACT: Artifact = {
  id: 'mk-1',
  name: 'markets-NVDA-20261006T041200Z.json',
  mime: MARKETS_ARTIFACT_MIME,
  download_url: '/api/v1/artifacts/mk-1',
}

describe('createArtifactRenderer markets artifacts', () => {
  it('classifies the markets mime as "markets" and nothing near it', () => {
    expect(artifactCategory(MARKETS_ARTIFACT)).toBe('markets')
    expect(
      artifactCategory({ mime: `${MARKETS_ARTIFACT_MIME}; charset=utf-8`, name: 'x' } as never),
    ).toBe('markets')
    expect(
      artifactCategory({ mime: 'application/vnd.agentos.market+json', name: 'x.json' } as never),
    ).not.toBe('markets')
  })

  it('renders a mount placeholder in its own group, never a download target', () => {
    const { deps } = chartRendererDeps()
    const container = document.createElement('div')
    container.innerHTML = createArtifactRenderer(deps).renderArtifacts([
      MARKETS_ARTIFACT,
      TRIGGER_ARTIFACT,
    ])
    const host = container.querySelector<HTMLElement>('[data-markets-src]')
    expect(host?.classList.contains('msg-artifact-markets')).toBe(true)
    expect(host?.dataset.marketsSrc).toBe(
      '/api/v1/artifacts/mk-1?sessionKey=agent%3Amain%3Awebchat%3Atest&token=tok',
    )
    expect(host?.dataset.artifactCategory).toBe('markets')
    expect(host?.querySelector('.msg-artifact-markets__body')).not.toBeNull()
    expect(host?.querySelector('.msg-artifact-markets__status')).toHaveTextContent(
      'Loading markets…',
    )
    expect(container.querySelector('.msg-artifact-markets-group [data-markets-src]')).toBe(host)
    expect(container.querySelector('.msg-artifact-markets-group [data-trigger-src]')).toBeNull()
    expect(container.querySelector('[data-artifact-download]')).toBeNull()
  })
})

/* ── ticketed media + downloads (lib/artifact-tickets.ts) ──────────────── */

describe('artifactHasThumbnail', () => {
  it('is true only for the still formats the gateway previews', () => {
    expect(artifactHasThumbnail({ mime: 'image/png' })).toBe(true)
    expect(artifactHasThumbnail({ mime: 'IMAGE/JPEG' })).toBe(true)
    expect(artifactHasThumbnail({ mime: 'image/webp' })).toBe(true)
    expect(artifactHasThumbnail({ mime: 'image/gif' })).toBe(false)
    expect(artifactHasThumbnail({ mime: 'image/svg+xml' })).toBe(false)
    expect(artifactHasThumbnail({ mime: 'audio/wav' })).toBe(false)
  })
})

describe('createArtifactRenderer with tickets', () => {
  const IMAGE: Artifact = {
    id: 'i-1',
    name: 'plot.png',
    mime: 'image/png',
    download_url: '/api/v1/artifacts/i-1',
  }
  const GIF: Artifact = { id: 'g-1', name: 'loop.gif', mime: 'image/gif' }
  const AUDIO: Artifact = { id: 'a-1', name: 'clip.wav', mime: 'audio/wav' }
  const FILE: Artifact = { id: 'x-1', name: 'report.xlsx', mime: 'application/zip' }
  const SESSION = 'agent%3Amain%3Awebchat%3Atest'

  function render(resolveTicket: ArtifactRendererDeps['resolveTicket'], artifacts: Artifact[]) {
    const { deps } = chartRendererDeps({ resolveTicket })
    const container = document.createElement('div')
    container.innerHTML = createArtifactRenderer(deps).renderArtifacts(artifacts)
    return container
  }

  it('leaves image and audio sources for the mount pass to fill', () => {
    const container = render(vi.fn(), [IMAGE, GIF, AUDIO])
    const [still, animated] = Array.from(container.querySelectorAll('img'))
    expect(still).not.toHaveAttribute('src')
    expect(still).toHaveAttribute('data-ticket-src', `/api/v1/artifacts/i-1?sessionKey=${SESSION}`)
    expect(still).toHaveAttribute('data-ticket-variant', 'thumbnail')
    // A GIF previews as itself.
    expect(animated).not.toHaveAttribute('data-ticket-variant')
    const audio = container.querySelector('audio')
    expect(audio).not.toHaveAttribute('src')
    expect(audio).toHaveAttribute('data-ticket-src', `/api/v1/artifacts/a-1?sessionKey=${SESSION}`)
    // The download targets the renderer's other surfaces read are unchanged.
    expect(container.querySelector('.msg-artifact-card--image')).toHaveAttribute(
      'data-artifact-download',
      '/api/v1/artifacts/i-1',
    )
  })

  it('keeps the auth token out of every URL on the page', () => {
    const container = render(vi.fn(), [IMAGE, AUDIO, FILE, CHART_ARTIFACT])
    expect(container.innerHTML).not.toContain('token=')
    expect(container.querySelector<HTMLElement>('[data-chart-src]')?.dataset.chartSrc).toBe(
      `/api/v1/artifacts/art-1?sessionKey=${SESSION}`,
    )
  })

  it('keeps direct sources without a resolver', () => {
    const container = render(undefined, [IMAGE, AUDIO])
    expect(container.querySelector('img')).toHaveAttribute(
      'src',
      `/api/v1/artifacts/i-1?sessionKey=${SESSION}&token=tok`,
    )
    expect(container.querySelector('[data-ticket-src]')).toBeNull()
  })
})

describe('downloadArtifact with tickets', () => {
  const REPORT: Artifact = { id: 'x-1', name: 'report.xlsx' }
  let clicked: string[]
  let clickSpy: { mockRestore: () => void }

  beforeEach(() => {
    clicked = []
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push(`${this.getAttribute('href')} ${this.download}`)
    })
    ;(URL as unknown as { createObjectURL: unknown }).createObjectURL = vi
      .fn()
      .mockReturnValue('blob:x')
    ;(URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn()
  })
  afterEach(() => {
    clickSpy.mockRestore()
    vi.unstubAllGlobals()
    delete window.__AGENTOS_ENV__
  })

  it('hands a ticket straight to the browser on the gateway-served console', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const resolveTicket = vi.fn().mockResolvedValue('/api/v1/artifacts/download/c/t')
    const { deps } = chartRendererDeps({ resolveTicket })

    await createArtifactRenderer(deps).downloadArtifact(REPORT)

    expect(resolveTicket).toHaveBeenCalledWith('/api/v1/artifacts/x-1', {
      sessionKey: 'agent:main:webchat:test',
    })
    expect(clicked).toEqual(['/api/v1/artifacts/download/c/t report.xlsx'])
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('fetches the ticket without a header off gateway', async () => {
    window.__AGENTOS_ENV__ = { apiOrigin: 'http://127.0.0.1:18791' }
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(['x']) })
    vi.stubGlobal('fetch', fetchMock)
    const resolveTicket = vi
      .fn()
      .mockResolvedValue('http://127.0.0.1:18791/api/v1/artifacts/download/c/t')
    const { deps } = chartRendererDeps({ resolveTicket })

    await createArtifactRenderer(deps).downloadArtifact(REPORT)

    expect(fetchMock).toHaveBeenCalledWith('http://127.0.0.1:18791/api/v1/artifacts/download/c/t', {
      method: 'GET',
      headers: {},
      credentials: 'same-origin',
    })
    expect(clicked).toEqual(['blob:x report.xlsx'])
  })

  it('falls back to the authenticated fetch when no ticket can be had', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(['x']) })
    vi.stubGlobal('fetch', fetchMock)
    const { deps } = chartRendererDeps({ resolveTicket: vi.fn().mockResolvedValue(null) })

    await createArtifactRenderer(deps).downloadArtifact(REPORT)

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/artifacts/x-1?sessionKey='),
      expect.objectContaining({
        headers: {
          Authorization: 'Bearer tok',
          'x-agentos-session-key': 'agent:main:webchat:test',
        },
      }),
    )
  })

  it('tells the user when the gateway refuses the file', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const toast = vi.fn()
    const { deps } = chartRendererDeps({
      resolveTicket: vi.fn().mockRejectedValue(new Error('artifact not found')),
      toast,
    })

    await createArtifactRenderer(deps).downloadArtifact(REPORT)

    expect(toast).toHaveBeenCalledWith('Download failed: artifact not found', 'warn', 3500)
    expect(fetchMock).not.toHaveBeenCalled()
    expect(clicked).toEqual([])
  })
})
