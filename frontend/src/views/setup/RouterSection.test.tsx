import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { RouterSection } from './RouterSection'
import type { Catalog } from './logic'

const STATUS = {
  hasConfig: true,
  llmConfigured: true,
}

const CONFIG = {
  llm: { provider: 'openai', model: 'gpt-4o' },
  agentos_router: { enabled: true, strategy: 'pilot-v1', default_tier: 'c1' },
}

function catalogWithTiers(tiers: Record<string, Record<string, unknown>>): Catalog {
  return {
    routerProfiles: {
      defaultTier: 'c1',
      profiles: [
        {
          // Keep the production gateway profile shape, including fields the
          // editor does not consume.
          profileId: 'openai',
          providerId: 'openai',
          label: 'OpenAI',
          tiers,
        },
      ],
      judge: {
        profiles: {
          openai: { autoModel: 'gpt-4o-mini', models: ['gpt-4o-mini', 'gpt-4o'] },
        },
      },
    },
  }
}

function renderSection(catalog: Catalog, onSave = vi.fn()) {
  const props = {
    catalog,
    status: STATUS,
    config: CONFIG,
    onSave,
    onBack: vi.fn(),
    onNext: vi.fn(),
    saving: false,
  }
  const result = render(<RouterSection {...props} />)
  return {
    ...result,
    rerenderCatalog: (nextCatalog: Catalog) =>
      result.rerender(<RouterSection {...props} catalog={nextCatalog} />),
  }
}

describe('RouterSection', () => {
  it('seeds tiers that appear in a later partial-catalog update without crashing', () => {
    const partialCatalog = catalogWithTiers({
      c0: { provider: 'openai', model: 'gpt-4o-mini' },
    })
    const view = renderSection(partialCatalog)

    expect(screen.getByLabelText('c0 model')).toHaveValue('gpt-4o-mini')
    expect(screen.queryByLabelText('c1 provider')).not.toBeInTheDocument()

    view.rerenderCatalog(
      catalogWithTiers({
        c0: { provider: 'openai', model: 'gpt-4o-mini' },
        c1: { provider: 'openai', model: 'gpt-4o' },
        image_model: { provider: 'openai', model: 'gpt-image-1' },
      }),
    )

    expect(screen.getByLabelText('c1 provider')).toHaveValue('openai')
    expect(screen.getByLabelText('c1 model')).toHaveValue('gpt-4o')
    expect(screen.getByLabelText('image_model model')).toHaveValue('gpt-image-1')
  })

  it('uses newly visible tier defaults when saving and keeps existing edits', () => {
    const onSave = vi.fn()
    const view = renderSection(
      catalogWithTiers({
        c0: { provider: 'openai', model: 'gpt-4o-mini' },
      }),
      onSave,
    )
    fireEvent.change(screen.getByLabelText('c0 model'), { target: { value: 'edited-c0' } })

    view.rerenderCatalog(
      catalogWithTiers({
        c0: { provider: 'openai', model: 'gpt-4o-mini' },
        c1: { provider: 'openai', model: 'gpt-4o' },
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save Router' }))

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        tiers: expect.objectContaining({
          c0: expect.objectContaining({ model: 'edited-c0' }),
          c1: expect.objectContaining({ provider: 'openai', model: 'gpt-4o' }),
        }),
      }),
    )
  })

  it('renders unified selects and accessible image capability controls', () => {
    renderSection(
      catalogWithTiers({
        c0: { provider: 'openai', model: 'gpt-4o-mini' },
        image_model: { provider: 'openai', model: 'gpt-image-1' },
      }),
    )

    expect(screen.getByLabelText('Router mode').parentElement).toHaveClass('setup-select')
    expect(screen.getAllByRole('columnheader')).toHaveLength(5)

    const capability = screen.getByLabelText('c0 supports image')
    expect(capability).not.toBeChecked()
    expect(capability).toHaveClass('setup-check__input')
    fireEvent.click(capability)
    expect(capability).toBeChecked()

    expect(screen.getByLabelText('image_model supports image')).toBeChecked()
    expect(screen.getByLabelText('image_model supports image')).toBeDisabled()
  })
})
