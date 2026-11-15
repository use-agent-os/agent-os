// @vitest-environment node
import { describe, expect, it, vi } from 'vitest'

// Only the pure template builder is under test; electron is reached at
// right-click time, never here.
vi.mock('electron', () => ({ clipboard: {}, Menu: {}, shell: {} }))

import { contextMenuTemplate, type ContextMenuActions, type ContextMenuInput } from './context-menu'

const NO_EDIT = {
  canUndo: false,
  canRedo: false,
  canCut: false,
  canCopy: false,
  canPaste: false,
  canDelete: false,
  canSelectAll: false,
  canEditRichly: false,
}

function input(over: Partial<ContextMenuInput> = {}): ContextMenuInput {
  return {
    isEditable: false,
    selectionText: '',
    linkURL: '',
    mediaType: 'none',
    hasImageContents: false,
    misspelledWord: '',
    dictionarySuggestions: [],
    editFlags: NO_EDIT,
    x: 10,
    y: 20,
    ...over,
  }
}

function actions(): ContextMenuActions & {
  [K in keyof ContextMenuActions]: ReturnType<typeof vi.fn>
} {
  return {
    replaceMisspelling: vi.fn(),
    addWordToDictionary: vi.fn(),
    copyImageAt: vi.fn(),
    copyText: vi.fn(),
    openExternal: vi.fn(),
    isAllowedExternalUrl: vi.fn((url: string) => url.startsWith('https:')),
  }
}

/** Fire a menu item's click as Electron would. */
function click(item: ReturnType<typeof contextMenuTemplate>[number] | undefined): void {
  item?.click?.({} as never, undefined, {} as never)
}

/** A readable shape of the menu: roles, labels and separators. */
function shape(items: ReturnType<typeof contextMenuTemplate>): string[] {
  return items.map((item) => (item.type === 'separator' ? '---' : (item.role ?? item.label ?? '?')))
}

describe('contextMenuTemplate', () => {
  it('offers Copy for selected chat text', () => {
    expect(
      shape(contextMenuTemplate(input({ selectionText: 'We hold 7.57%' }), actions())),
    ).toEqual(['copy'])
  })

  it('shows nothing when there is nothing to act on', () => {
    expect(contextMenuTemplate(input(), actions())).toEqual([])
    expect(contextMenuTemplate(input({ selectionText: '  \n ' }), actions())).toEqual([])
  })

  it('gives a text field the edit menu, enabled by its edit flags', () => {
    const items = contextMenuTemplate(
      input({ isEditable: true, editFlags: { ...NO_EDIT, canPaste: true, canSelectAll: true } }),
      actions(),
    )
    expect(shape(items)).toEqual(['undo', 'redo', '---', 'cut', 'copy', 'paste', 'selectAll'])
    const enabled = Object.fromEntries(items.filter((i) => i.role).map((i) => [i.role, i.enabled]))
    expect(enabled).toEqual({
      undo: false,
      redo: false,
      cut: false,
      copy: false,
      paste: true,
      selectAll: true,
    })
  })

  it('copies and opens a web link', () => {
    const a = actions()
    const items = contextMenuTemplate(
      input({ linkURL: 'https://basescan.org/tx/0xabc', selectionText: 'tx' }),
      a,
    )
    expect(shape(items)).toEqual(['Open Link in Browser', 'Copy Link', '---', 'copy'])
    click(items[0])
    click(items[1])
    expect(a.openExternal).toHaveBeenCalledWith('https://basescan.org/tx/0xabc')
    expect(a.copyText).toHaveBeenCalledWith('https://basescan.org/tx/0xabc')
  })

  it('never offers to open a link the OS must not receive', () => {
    const items = contextMenuTemplate(input({ linkURL: 'file:///etc/passwd' }), actions())
    expect(shape(items)).toEqual(['Copy Link'])
  })

  it('copies an image at the click point', () => {
    const a = actions()
    const items = contextMenuTemplate(input({ mediaType: 'image', hasImageContents: true }), a)
    expect(shape(items)).toEqual(['Copy Image'])
    click(items[0])
    expect(a.copyImageAt).toHaveBeenCalledWith(10, 20)
  })

  it('puts spelling suggestions above the edit menu', () => {
    const a = actions()
    const items = contextMenuTemplate(
      input({
        isEditable: true,
        misspelledWord: 'tweeet',
        dictionarySuggestions: ['tweet', 'tweets'],
      }),
      a,
    )
    expect(shape(items).slice(0, 4)).toEqual(['tweet', 'tweets', 'Learn Spelling', '---'])
    click(items[0])
    click(items[2])
    expect(a.replaceMisspelling).toHaveBeenCalledWith('tweet')
    expect(a.addWordToDictionary).toHaveBeenCalledWith('tweeet')
  })

  it('says so when a misspelling has no guesses', () => {
    const items = contextMenuTemplate(
      input({ isEditable: true, misspelledWord: 'zzqx' }),
      actions(),
    )
    expect(items[0]).toMatchObject({ label: 'No Guesses Found', enabled: false })
  })
})
