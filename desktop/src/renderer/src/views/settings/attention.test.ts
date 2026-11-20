import { describe, expect, it } from 'vitest'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { sectionAttention, SECTION_FOR_ONBOARDING } from './attention'

function snapshot(details: Record<string, Record<string, unknown>>): SettingsSnapshot {
  return { status: { sectionDetails: details } }
}

describe('sectionAttention', () => {
  it('marks nothing when every section is fine or merely optional', () => {
    expect(
      sectionAttention(
        snapshot({
          llm: { status: 'ok', actionRequired: false },
          channels: { status: 'optional', actionRequired: false },
        }),
      ),
    ).toEqual({})
    expect(sectionAttention(undefined)).toEqual({})
  })

  it('a blocking provider is danger on Providers and on Overview', () => {
    const out = sectionAttention(
      snapshot({ llm: { status: 'missing', actionRequired: true, blocking: true } }),
    )
    expect(out).toEqual({ providers: 'danger', overview: 'danger' })
  })

  it('search, image and voice share Capabilities; Overview takes the worst tone', () => {
    const out = sectionAttention(
      snapshot({
        search: { status: 'degraded', actionRequired: true, blocking: false },
        audio: { status: 'degraded', actionRequired: true, blocking: false },
        llm: { status: 'degraded', actionRequired: true, blocking: true },
      }),
    )
    expect(out.capabilities).toBe('warn')
    expect(out.providers).toBe('danger')
    expect(out.overview).toBe('danger')
  })

  it('falls back to the readiness details and ignores sections it does not own', () => {
    const out = sectionAttention({
      readiness: {
        sectionDetails: {
          memory_embedding: { status: 'degraded', actionRequired: true },
          something_new: { status: 'missing', actionRequired: true },
        },
      },
    })
    expect(out).toEqual({ memory: 'warn', overview: 'warn' })
    expect(SECTION_FOR_ONBOARDING.memory_embedding).toBe('memory')
  })
})
