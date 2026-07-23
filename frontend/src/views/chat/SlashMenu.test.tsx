import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { normalizeSlashCommand } from './logic'
import { SlashMenu, type SlashMenuHandle } from './SlashMenu'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

// The normalized catalog (as useSlashCommands hands it to the menu). The menu is
// a pure renderer over `commands` — the RPC load lives in useSlashCommands.
const CATALOG = [
  { name: '/help', usage: '/help', description: 'Show the command list', aliases: [] },
  { name: '/new', usage: '/new', description: 'Start a new chat', aliases: [] },
  {
    name: '/compact',
    usage: '/compact',
    description: 'Compact the context',
    aliases: [],
    execution: { action: 'compact_context' },
  },
  { name: '/reset', usage: '/reset', description: 'Reset the session', aliases: [] },
].map(normalizeSlashCommand)

function renderMenu(props: {
  value: string
  onExecute?: (text: string) => void
  onClose?: () => void
  handleRef?: React.Ref<SlashMenuHandle>
}) {
  return render(
    <SlashMenu
      value={props.value}
      commands={CATALOG}
      onExecute={props.onExecute ?? (() => {})}
      onClose={props.onClose ?? (() => {})}
      handleRef={props.handleRef}
    />,
  )
}

describe('SlashMenu', () => {
  it('opens and filters the slash menu on a "/" prefix (chat.js:2643)', async () => {
    const { rerender } = renderMenu({ value: '/' })
    // All commands visible for a bare "/".
    expect(await screen.findByText('/help')).toBeInTheDocument()
    expect(screen.getByText('/new')).toBeInTheDocument()
    // Narrow to "/he" → only /help survives the prefix filter.
    rerender(<SlashMenu value="/he" commands={CATALOG} onExecute={() => {}} onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText('/help')).toBeInTheDocument())
    expect(screen.queryByText('/new')).not.toBeInTheDocument()
    expect(screen.queryByText('/compact')).not.toBeInTheDocument()
  })

  it('does NOT open for the "//" literal-slash escape (chat.js:2640)', () => {
    renderMenu({ value: '//help' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(screen.queryByText('/help')).not.toBeInTheDocument()
  })

  it('renders nothing when the filter matches no command (chat.js:2644)', () => {
    renderMenu({ value: '/zzz' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('moves the active item with ArrowDown/ArrowUp via the keyboard handle (chat.js:2654-2662)', async () => {
    const handleRef = createRef<SlashMenuHandle>()
    renderMenu({ value: '/', handleRef })
    await screen.findByText('/help')
    // First item active by default (chat.js:2646 _slashIdx = 0).
    const options = () => screen.getAllByRole('option')
    expect(options()[0]).toHaveAttribute('aria-selected', 'true')
    act(() => {
      handleRef.current!.handleKeyDown({ key: 'ArrowDown', preventDefault() {} } as never)
    })
    expect(options()[1]).toHaveAttribute('aria-selected', 'true')
    expect(options()[0]).toHaveAttribute('aria-selected', 'false')
    act(() => {
      handleRef.current!.handleKeyDown({ key: 'ArrowUp', preventDefault() {} } as never)
    })
    expect(options()[0]).toHaveAttribute('aria-selected', 'true')
  })

  it('assigns stable listbox/option IDs and reports the active descendant', async () => {
    const onActiveDescendantChange = vi.fn()
    const handleRef = createRef<SlashMenuHandle>()
    render(
      <SlashMenu
        value="/"
        commands={CATALOG}
        onExecute={() => {}}
        handleRef={handleRef}
        listboxId="slash-commands"
        onActiveDescendantChange={onActiveDescendantChange}
      />,
    )

    expect(await screen.findByRole('listbox')).toHaveAttribute('id', 'slash-commands')
    expect(screen.getAllByRole('option')[0]).toHaveAttribute('id', 'slash-commands-option-0')
    await waitFor(() =>
      expect(onActiveDescendantChange).toHaveBeenLastCalledWith('slash-commands-option-0'),
    )

    act(() => {
      handleRef.current!.handleKeyDown({ key: 'ArrowDown', preventDefault() {} } as never)
    })
    await waitFor(() =>
      expect(onActiveDescendantChange).toHaveBeenLastCalledWith('slash-commands-option-1'),
    )
  })

  it('Enter executes the active command (chat.js:2851 _selectSlashCmd)', async () => {
    const onExecute = vi.fn()
    const handleRef = createRef<SlashMenuHandle>()
    renderMenu({ value: '/comp', onExecute, handleRef })
    await screen.findByText('/compact')
    act(() => {
      handleRef.current!.handleKeyDown({ key: 'Enter', preventDefault() {} } as never)
    })
    // The active command's text is dispatched for execution (chat.js:2842).
    expect(onExecute).toHaveBeenCalledWith('/compact')
  })

  it('a click on an item executes that command (chat.js:2669)', async () => {
    const onExecute = vi.fn()
    renderMenu({ value: '/', onExecute })
    const item = await screen.findByText('/new')
    fireEvent.click(item)
    expect(onExecute).toHaveBeenCalledWith('/new')
  })

  it('Escape closes the menu (chat.js:2675 _closeSlashMenu)', async () => {
    const onClose = vi.fn()
    const handleRef = createRef<SlashMenuHandle>()
    renderMenu({ value: '/', onClose, handleRef })
    await screen.findByText('/help')
    let handled = false
    act(() => {
      handled = handleRef.current!.handleKeyDown({ key: 'Escape', preventDefault() {} } as never)
    })
    expect(handled).toBe(true)
    expect(onClose).toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })

  it('the keyboard handle is inert (returns false) when the menu is closed', () => {
    const handleRef = createRef<SlashMenuHandle>()
    renderMenu({ value: 'plain text', handleRef })
    // Menu is closed → Enter/Arrow keys are not intercepted (composer keeps them).
    expect(handleRef.current!.handleKeyDown({ key: 'Enter', preventDefault() {} } as never)).toBe(
      false,
    )
    expect(handleRef.current!.isOpen()).toBe(false)
  })
})
