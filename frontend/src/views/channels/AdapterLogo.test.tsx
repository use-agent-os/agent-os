import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AdapterLogo } from './AdapterLogo'

describe('AdapterLogo', () => {
  it.each([
    ['dingtalk', 'dingtalk'],
    ['discord', 'discord'],
    ['matrix', 'matrix'],
    ['qq', 'qq'],
    ['slack', 'slack'],
    ['telegram', 'telegram'],
    ['wecom', 'wechat'],
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
})
