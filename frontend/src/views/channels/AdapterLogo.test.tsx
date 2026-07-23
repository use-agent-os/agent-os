import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AdapterLogo } from './AdapterLogo'

describe('AdapterLogo', () => {
  it.each([
    ['discord', 'discord'],
    ['slack', 'slack'],
    ['telegram', 'telegram'],
    ['msteams', 'teams'],
  ])('renders the %s brand mark', (type, logoKey) => {
    const { container } = render(<AdapterLogo type={type} />)
    const logo = container.querySelector(`[data-adapter-logo="${logoKey}"]`)
    expect(logo).toBeInTheDocument()
    expect(logo).toHaveAttribute('aria-hidden', 'true')
    expect(logo).toHaveAttribute('focusable', 'false')
  })

  it('uses a deterministic generic mark for an unknown adapter', () => {
    const { container } = render(<AdapterLogo type="future-channel" />)
    expect(container.querySelector('[data-adapter-logo="generic"]')).toBeInTheDocument()
  })

  it.each(['dingtalk', 'matrix', 'qq', 'qqbot', 'wecom'])(
    'does not retain a retired %s brand mapping',
    (type) => {
      const { container } = render(<AdapterLogo type={type} />)
      expect(container.querySelector('[data-adapter-logo="generic"]')).toBeInTheDocument()
    },
  )
})
