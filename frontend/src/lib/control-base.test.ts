import { describe, expect, it } from 'vitest'
import { controlBasePath, controlPath, deriveControlBasePath } from './control-base'

function taggedDocument(controlBase: string | null, href: string): Document {
  const doc = document.implementation.createHTMLDocument('AgentOS Control')
  const tag = doc.createElement('base')
  tag.setAttribute('href', href)
  tag.setAttribute('data-agentos-control-base', controlBase ?? '')
  doc.head.append(tag)
  return doc
}

describe('Control UI runtime base path', () => {
  it('uses the explicit /control mount independently from its static asset href', () => {
    const doc = taggedDocument('/control', '/control/static/dist/')

    expect(
      controlBasePath({
        document: doc,
        documentUrl: 'https://agent.example/control/',
        buildBase: './',
      }),
    ).toBe('/control')
    expect(controlPath('api/bootstrap', '/control')).toBe('/control/api/bootstrap')
  })

  it('supports a custom /console mount on a deep-linked route', () => {
    const doc = taggedDocument('/console/', '/console/static/dist/')

    expect(
      controlBasePath({
        document: doc,
        documentUrl: 'https://agent.example/console/mcp/oauth/callback?code=ok',
        buildBase: './',
      }),
    ).toBe('/console')
    expect(controlPath('/api/bootstrap', '/console')).toBe('/console/api/bootstrap')
  })

  it('derives the mount from a valueless production tag on a deep link', () => {
    expect(
      deriveControlBasePath({
        documentUrl: 'https://agent.example/console/settings/router',
        taggedControlBase: '',
        taggedBaseHref: '/console/static/dist/',
        buildBase: './',
      }),
    ).toBe('/console')
  })

  it('falls back to Vite dev base when no production tag is present', () => {
    expect(
      deriveControlBasePath({
        documentUrl: 'http://localhost:5173/control/chat',
        buildBase: '/control/',
      }),
    ).toBe('/control')
  })
})
