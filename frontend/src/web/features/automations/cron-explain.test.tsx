import '@/test/web'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { humanTime } from '@/views/cron/logic'
import { CronExplain } from './AutomationEditor'

afterEach(() => vi.useRealTimers())

describe('CronExplain', () => {
  it('previews the runs the scheduler makes: an empty timezone is UTC', () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(Date.UTC(2026, 10, 10, 4, 0))
    const { rerender } = render(<CronExplain expr="0 9 * * *" timeZone="" />)
    const first = screen.getAllByRole('listitem')[0]!
    expect(first).toHaveTextContent(humanTime(new Date(Date.UTC(2026, 10, 10, 9, 0))))
    rerender(<CronExplain expr="0 9 * * *" timeZone="Asia/Ho_Chi_Minh" />)
    expect(screen.getAllByRole('listitem')[0]!).toHaveTextContent(
      humanTime(new Date(Date.UTC(2026, 10, 11, 2, 0))),
    )
  })
})
