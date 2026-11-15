import {
  clipboard,
  Menu,
  shell,
  type BrowserWindow,
  type ContextMenuParams,
  type MenuItemConstructorOptions,
} from 'electron'

/** What the template needs from the page under the cursor. */
export type ContextMenuInput = Pick<
  ContextMenuParams,
  | 'isEditable'
  | 'selectionText'
  | 'linkURL'
  | 'mediaType'
  | 'hasImageContents'
  | 'misspelledWord'
  | 'dictionarySuggestions'
  | 'editFlags'
  | 'x'
  | 'y'
>

/** The page-side actions a menu item may trigger. */
export interface ContextMenuActions {
  replaceMisspelling(word: string): void
  addWordToDictionary(word: string): void
  copyImageAt(x: number, y: number): void
  copyText(text: string): void
  openExternal(url: string): void
  isAllowedExternalUrl(url: string): boolean
}

/**
 * The right-click menu for a point in the renderer. Electron shows nothing
 * by default, so without this a selection in the chat could only be copied
 * with ⌘C. Roles (`copy`, `paste`, …) act on the focused page, the same as
 * the Edit menu. Returns an empty list when there is nothing to offer, in
 * which case no menu is shown. A renderer component that draws its own menu
 * (`onContextMenu` + `preventDefault`) never reaches this: Chromium does not
 * ask the browser for a menu once the DOM event is cancelled.
 */
export function contextMenuTemplate(
  params: ContextMenuInput,
  actions: ContextMenuActions,
): MenuItemConstructorOptions[] {
  const groups: MenuItemConstructorOptions[][] = []
  const hasSelection = params.selectionText.trim().length > 0

  if (params.isEditable && params.misspelledWord) {
    const suggestions: MenuItemConstructorOptions[] = params.dictionarySuggestions
      .slice(0, 5)
      .map((word) => ({ label: word, click: () => actions.replaceMisspelling(word) }))
    if (suggestions.length === 0) suggestions.push({ label: 'No Guesses Found', enabled: false })
    const word = params.misspelledWord
    suggestions.push({
      label: 'Learn Spelling',
      click: () => actions.addWordToDictionary(word),
    })
    groups.push(suggestions)
  }

  if (params.linkURL) {
    const url = params.linkURL
    const link: MenuItemConstructorOptions[] = []
    if (actions.isAllowedExternalUrl(url)) {
      link.push({ label: 'Open Link in Browser', click: () => actions.openExternal(url) })
    }
    link.push({ label: 'Copy Link', click: () => actions.copyText(url) })
    groups.push(link)
  }

  if (params.mediaType === 'image' && params.hasImageContents) {
    groups.push([{ label: 'Copy Image', click: () => actions.copyImageAt(params.x, params.y) }])
  }

  if (params.isEditable) {
    const flags = params.editFlags
    groups.push(
      [
        { role: 'undo', enabled: flags.canUndo },
        { role: 'redo', enabled: flags.canRedo },
      ],
      [
        { role: 'cut', enabled: flags.canCut },
        { role: 'copy', enabled: flags.canCopy },
        { role: 'paste', enabled: flags.canPaste },
        { role: 'selectAll', enabled: flags.canSelectAll },
      ],
    )
  } else if (hasSelection) {
    groups.push([{ role: 'copy' }])
  }

  return groups.flatMap((group, i) =>
    i === 0 ? group : [{ type: 'separator' } as MenuItemConstructorOptions, ...group],
  )
}

/** Show `contextMenuTemplate` on every right-click in this window. */
export function installContextMenu(
  win: BrowserWindow,
  isAllowedExternalUrl: (url: string) => boolean,
): void {
  const contents = win.webContents
  contents.on('context-menu', (_event, params) => {
    const template = contextMenuTemplate(params, {
      replaceMisspelling: (word) => contents.replaceMisspelling(word),
      addWordToDictionary: (word) => contents.session.addWordToSpellCheckerDictionary(word),
      copyImageAt: (x, y) => contents.copyImageAt(x, y),
      copyText: (text) => clipboard.writeText(text),
      openExternal: (url) => void shell.openExternal(url),
      isAllowedExternalUrl,
    })
    if (template.length > 0) Menu.buildFromTemplate(template).popup({ window: win })
  })
}
