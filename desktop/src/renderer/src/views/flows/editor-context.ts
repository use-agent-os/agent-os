import { createContext, useContext } from 'react'
import type { EditorStore } from './editor-store'
import type { ImageRef, ModelsPayload, NodeSchema, NodesPayload } from './types'

/**
 * What every node on the canvas needs beyond its own data: the store, the
 * schema and model registry, and the editor's actions that open something
 * outside the node (the model picker, the image viewer, a file upload).
 */
export interface EditorEnv {
  store: EditorStore
  schemas: ReadonlyMap<string, NodeSchema>
  nodes: NodesPayload
  models: ModelsPayload | undefined
  openModelPicker: (nodeId: string, anchor: DOMRect) => void
  openImages: (images: ImageRef[], index: number) => void
  uploadImage: (nodeId: string, input: string, file: File) => Promise<void>
  /** The gateway refuses this window's writes: nodes show, nothing edits. */
  readOnly: boolean
}

export const EditorEnvContext = createContext<EditorEnv | null>(null)

export function useEditorEnv(): EditorEnv {
  const env = useContext(EditorEnvContext)
  if (!env) throw new Error('Flow nodes render inside the flow editor')
  return env
}

/** CSS colour for a link type: the theme's own where it has one, else the schema's. */
export function typeColor(type: string | null | undefined, nodes?: NodesPayload): string {
  if (type === 'TEXT') return 'var(--flw-text)'
  if (type === 'IMAGE') return 'var(--flw-image)'
  return (type && nodes?.types?.[type]?.color) || 'var(--dim)'
}
