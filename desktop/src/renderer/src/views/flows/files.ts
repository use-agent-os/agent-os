import type { ImageRef, NodeOutputs } from './types'

/** Image files dropped or picked onto the canvas, and images saved back to disk. */

export const MAX_IMAGE_BYTES = 25 * 1024 * 1024

export const DROP_MIMES: ReadonlySet<string> = new Set(['image/png', 'image/jpeg'])

export const UPLOAD_ACCEPT = 'image/png,image/jpeg,image/webp'

/** The file's bytes as base64 (no data: prefix). */
export function fileToBase64(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(reader.error ?? new Error('read failed'))
    reader.onload = () => {
      const url = String(reader.result ?? '')
      const comma = url.indexOf(',')
      resolve(comma >= 0 ? url.slice(comma + 1) : url)
    }
    reader.readAsDataURL(file)
  })
}

/** PNG and JPEG by type, or by name when the OS left the type empty. */
export function droppedImageMime(file: File): string | null {
  if (DROP_MIMES.has(file.type)) return file.type
  const name = file.name.toLowerCase()
  if (!file.type && name.endsWith('.png')) return 'image/png'
  if (!file.type && (name.endsWith('.jpg') || name.endsWith('.jpeg'))) return 'image/jpeg'
  return null
}

/** How long a download's object URL outlives the click; the Save dialog can stay open a while. */
const DOWNLOAD_URL_TTL_MS = 60_000

const MIME_EXTENSIONS: Readonly<Record<string, string>> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/webp': 'webp',
}

/** The name to offer for a blob no Save node has named. */
export function imageFileName(image: Pick<ImageRef, 'sha256' | 'mime'>): string {
  const ext = MIME_EXTENSIONS[image.mime] ?? 'png'
  return `agentos-flow-${image.sha256.slice(0, 8)}.${ext}`
}

/** The file each image of one node's outputs was written to, by blob (Save lists both in order). */
export function savedFileNames(outputs: NodeOutputs | undefined): Record<string, string> {
  const names: Record<string, string> = {}
  outputs?.images?.forEach((image, i) => {
    const name = outputs.files?.[i]?.split(/[\\/]/).pop()
    if (name) names[image.sha256] = name
  })
  return names
}

/** {@link savedFileNames} across a whole run. */
export function runFileNames(
  outputs: Readonly<Record<string, NodeOutputs>>,
): Record<string, string> {
  return Object.assign({}, ...Object.values(outputs).map(savedFileNames)) as Record<string, string>
}

export function base64ToBlob(dataB64: string, mime: string): Blob {
  const bytes = Uint8Array.from(atob(dataB64), (c) => c.charCodeAt(0))
  return new Blob([bytes], { type: mime })
}

/** Hand a blob to the shell's download path, which asks where to save it. */
export function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = fileName
  link.hidden = true
  document.body.append(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_URL_TTL_MS)
}
