/** Image files dropped or picked onto the canvas. */

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
