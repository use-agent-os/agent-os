import '@/test/web'
import { render, renderHook, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Search } from 'lucide-react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { Badge, StatusDot } from './badge'
import { Card, StatCard } from './card'
import { Input, Select, Textarea } from './input'
import { Kbd } from './kbd'
import { SectionHeader } from './section-header'
import { SegmentedControl } from './segmented-control'
import { useCountUp } from './use-count-up'

describe('fields', () => {
  it('draws inputs, textareas and selects with the control boundary base.css refines', () => {
    render(
      <>
        <Input aria-label="Name" aria-invalid />
        <Textarea aria-label="Notes" />
        <Select aria-label="Agent" defaultValue="b">
          <option value="a">A</option>
          <option value="b">B</option>
        </Select>
      </>,
    )
    for (const el of [
      screen.getByRole('textbox', { name: 'Name' }),
      screen.getByRole('textbox', { name: 'Notes' }),
      screen.getByRole('combobox', { name: 'Agent' }),
    ]) {
      expect(el).toHaveClass('border-border-control', 'aria-[invalid=true]:border-danger')
    }
    expect(screen.getByRole('combobox', { name: 'Agent' })).toHaveValue('b')
  })

  it('puts a decorative leading icon in a wrapper that takes the layout classes', () => {
    render(<Input aria-label="Search" icon={<Search />} wrapperClassName="flex-1" />)
    const input = screen.getByRole('textbox', { name: 'Search' })
    expect(input).toHaveClass('pl-8')
    expect(input.parentElement).toHaveClass('relative', 'flex-1')
    expect(input.parentElement!.querySelector('[aria-hidden]')).not.toBeNull()
  })
})

function Choice({ kind }: { kind: 'radio' | 'toggle' }) {
  const [value, setValue] = useState<'day' | 'week'>('day')
  return (
    <SegmentedControl
      aria-label="Range"
      kind={kind}
      value={value}
      onChange={setValue}
      options={[
        { value: 'day', label: 'Day' },
        { value: 'week', label: 'Week' },
      ]}
    />
  )
}

describe('SegmentedControl', () => {
  it('is a named radiogroup of native radios that marks the active item', async () => {
    const user = userEvent.setup()
    render(<Choice kind="radio" />)
    const group = screen.getByRole('radiogroup', { name: 'Range' })
    expect(group).toHaveClass('glide-track')
    expect(group).toHaveAttribute('data-segmented')
    await user.click(screen.getByRole('radio', { name: 'Week' }))
    expect(screen.getByRole('radio', { name: 'Week' })).toBeChecked()
    const label = screen.getByRole('radio', { name: 'Week' }).closest('label')!
    expect(label).toHaveAttribute('data-active', 'true')
    expect(label.className).toMatch(/forced-colors:bg-\[Highlight\]/)
  })

  it('renders aria-pressed buttons as a toggle group', async () => {
    const user = userEvent.setup()
    render(<Choice kind="toggle" />)
    expect(screen.getByRole('group', { name: 'Range' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Week' }))
    expect(screen.getByRole('button', { name: 'Week' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Day' })).toHaveAttribute('aria-pressed', 'false')
  })
})

describe('Card and StatCard', () => {
  it('renders the child element with card styling via asChild', () => {
    render(
      <Card asChild variant="interactive">
        <a href="/x">Open</a>
      </Card>,
    )
    const link = screen.getByRole('link', { name: 'Open' })
    expect(link).toHaveAttribute('data-slot', 'card')
    expect(link).toHaveClass('lift', 'rounded-card')
  })

  it('names the section by its label and shows a formatted numeric figure', () => {
    render(<StatCard label="Sessions" value={1234} hint="Stored sessions" />)
    const card = screen.getByRole('region', { name: 'Sessions' })
    expect(card).toHaveTextContent((1234).toLocaleString())
    expect(screen.getByRole('heading', { name: 'Sessions' })).toBeInTheDocument()
    expect(card).toHaveTextContent('Stored sessions')
  })

  it('shows a loading region instead of the figure while loading', () => {
    render(<StatCard label="Uptime" value="1h" loading loadingLabel="Loading uptime" />)
    expect(screen.getByRole('status', { name: 'Loading uptime' })).toBeInTheDocument()
    expect(screen.queryByText('1h')).toBeNull()
  })

  it('says a trend in words, not only colour', () => {
    render(
      <StatCard
        label="Spend"
        value="$4"
        trend={{ direction: 'down', label: '-12% vs last week' }}
      />,
    )
    expect(screen.getByText('-12% vs last week')).toHaveClass('text-danger')
  })
})

describe('useCountUp', () => {
  it('returns the target at once under reduced motion (the test default)', () => {
    const { result, rerender } = renderHook(({ n }) => useCountUp(n), { initialProps: { n: 42 } })
    expect(result.current).toBe(42)
    rerender({ n: 7 })
    expect(result.current).toBe(7)
  })
})

describe('Badge, StatusDot, Kbd, SectionHeader', () => {
  it('keeps a live dot pulsing under reduced motion via motion-status', () => {
    render(
      <>
        <Badge tone="ok" live>
          Running
        </Badge>
        <StatusDot tone="warn" />
      </>,
    )
    const badge = screen.getByText('Running')
    const live = badge.querySelector('.status-dot')!
    expect(live).toHaveAttribute('data-live')
    expect(live).toHaveClass('motion-status')
    const still = document.querySelectorAll('.status-dot')[1]!
    expect(still).not.toHaveAttribute('data-live')
    expect(still).not.toHaveClass('motion-status')
  })

  it('renders a kbd and a heading at the requested level', () => {
    render(
      <>
        <Kbd>K</Kbd>
        <SectionHeader
          as="h3"
          title="Models"
          description="Routing"
          actions={<button>Add</button>}
        />
      </>,
    )
    expect(screen.getByText('K').tagName).toBe('KBD')
    expect(screen.getByRole('heading', { level: 3, name: 'Models' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add' })).toBeInTheDocument()
  })
})

describe('cn', () => {
  it('keeps a named type size beside a text colour', async () => {
    const { cn } = await import('@/lib/utils')
    expect(cn('text-stat', 'text-danger')).toBe('text-stat text-danger')
    expect(cn('text-ui', 'text-text-muted')).toBe('text-ui text-text-muted')
    expect(cn('text-small', 'text-label')).toBe('text-label')
  })
})
