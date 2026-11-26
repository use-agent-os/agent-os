import { create } from 'zustand'

// What the Files tab was showing, per session: the open folders, the selected
// file, the filter and (in a narrow panel) whether the tree or the file is in
// front. Kept outside the component so hiding the panel, switching tabs or
// changing dock does not lose the place; it lasts for the life of the page.

export type FilesMode = 'tree' | 'file'

export interface FilesView {
  expanded: readonly string[]
  selected: string | null
  query: string
  mode: FilesMode
}

export const EMPTY_VIEW: FilesView = { expanded: [], selected: null, query: '', mode: 'tree' }

interface FilesState {
  views: Record<string, FilesView>
  update(key: string, patch: Partial<FilesView> | ((view: FilesView) => Partial<FilesView>)): void
}

export const useFilesStore = create<FilesState>((set) => ({
  views: {},
  update(key, patch) {
    set((state) => {
      const view = state.views[key] ?? EMPTY_VIEW
      const next = typeof patch === 'function' ? patch(view) : patch
      return { views: { ...state.views, [key]: { ...view, ...next } } }
    })
  },
}))

export function resetFilesForTests(): void {
  useFilesStore.setState({ views: {} })
}
