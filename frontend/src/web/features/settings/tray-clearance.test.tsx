import { render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Docked } from './tray-clearance'

const clearance = () => document.documentElement.style.getPropertyValue('--tray-clearance')

afterEach(() => vi.restoreAllMocks())

describe('tray clearance', () => {
  it('claims height plus bottom while a docked bar is shown, the largest one wins, and clears on unmount', () => {
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (
      this: HTMLElement,
    ) {
      return Number(this.dataset.h ?? 0)
    })
    const bar = render(<Docked data-h="52" style={{ bottom: '16px' }} />)
    expect(clearance()).toBe('68px')
    const notice = render(<Docked data-h="70" style={{ bottom: '16px' }} />)
    expect(clearance()).toBe('86px')
    notice.unmount()
    expect(clearance()).toBe('68px')
    bar.unmount()
    expect(clearance()).toBe('')
  })

  it('claims nothing while hidden', () => {
    const { unmount } = render(<Docked style={{ bottom: '16px' }} />)
    expect(clearance()).toBe('')
    unmount()
  })
})
