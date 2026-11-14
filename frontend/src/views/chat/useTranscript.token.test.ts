// AOS-11: the transcript's legacy (ticket-less) media and chart-payload paths
// read their URL off `data-*` hooks in the DOM. The gateway token may go only
// to the gateway: a foreign URL loads nothing and is fetched without it.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { TicketResolver } from '@/lib/artifact-tickets'
import { fetchChartPayload, legacyMediaUrl } from './useTranscript'

const ORIGIN = 'http://127.0.0.1:18791'
const FOREIGN = [
  'https://attacker.invalid/px',
  'http://127.0.0.1:18791.attacker.invalid/px',
  'http://127.0.0.1:18791@attacker.invalid/px',
  'https://127.0.0.1:18791/px',
]

const noTickets = { resolveUrl: vi.fn().mockResolvedValue(null) } as unknown as TicketResolver

beforeEach(() => {
  window.__AGENTOS_ENV__ = { apiOrigin: ORIGIN }
  sessionStorage.setItem('agentos.wsToken', 'DUMMY-GATEWAY-TOKEN')
})
afterEach(() => {
  delete window.__AGENTOS_ENV__
  sessionStorage.clear()
  vi.unstubAllGlobals()
})

describe('legacyMediaUrl', () => {
  it('appends the token to a gateway URL', () => {
    expect(legacyMediaUrl(`${ORIGIN}/api/v1/artifacts/7`)).toBe(
      `${ORIGIN}/api/v1/artifacts/7?token=DUMMY-GATEWAY-TOKEN`,
    )
  })

  it('loads nothing for a URL that is not the gateway', () => {
    for (const raw of FOREIGN) expect(legacyMediaUrl(raw), raw).toBe('')
  })
})

describe('fetchChartPayload', () => {
  it('sends the token to the gateway', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
    vi.stubGlobal('fetch', fetchMock)

    await fetchChartPayload(`${ORIGIN}/api/v1/artifacts/7`, noTickets)

    expect(fetchMock.mock.calls[0]?.[1].headers).toEqual({
      Authorization: 'Bearer DUMMY-GATEWAY-TOKEN',
    })
  })

  it('never sends the token anywhere else', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
    vi.stubGlobal('fetch', fetchMock)

    for (const raw of FOREIGN) await fetchChartPayload(raw, noTickets)

    for (const [, init] of fetchMock.mock.calls) expect(init.headers).toEqual({})
  })
})
