import { afterEach, describe, expect, it } from 'vitest'
import { apiOrigin, apiUrl, hostControlBase, urlBase } from './api-origin'

afterEach(() => {
  delete window.__AGENTOS_ENV__
})

describe('api-origin', () => {
  it('is same-origin relative when no host env is set', () => {
    expect(apiOrigin()).toBe('')
    expect(apiUrl('/api/approvals')).toBe('/api/approvals')
    expect(hostControlBase()).toBeNull()
    expect(urlBase()).toBe(window.location.origin)
  })

  it('prefixes gateway URLs with the host origin when set', () => {
    window.__AGENTOS_ENV__ = { apiOrigin: 'http://127.0.0.1:18791/', controlBase: '/control' }
    expect(apiOrigin()).toBe('http://127.0.0.1:18791')
    expect(apiUrl('api/v1/files/upload')).toBe('http://127.0.0.1:18791/api/v1/files/upload')
    expect(hostControlBase()).toBe('/control')
    expect(urlBase()).toBe('http://127.0.0.1:18791')
  })

  it('ignores a blank control base', () => {
    window.__AGENTOS_ENV__ = { controlBase: '  ' }
    expect(hostControlBase()).toBeNull()
  })
})
