import { describe, expect, it } from 'vitest'
import { base64ToBlob, imageFileName, runFileNames, savedFileNames } from './files'
import { IMG } from './test-fixtures'

const OTHER = { ...IMG, sha256: 'b'.repeat(64) }

describe('imageFileName', () => {
  it('names a blob by its hash and type', () => {
    expect(imageFileName(IMG)).toBe('agentos-flow-aaaaaaaa.png')
    expect(imageFileName({ ...IMG, mime: 'image/jpeg' })).toBe('agentos-flow-aaaaaaaa.jpg')
    expect(imageFileName({ ...IMG, mime: 'image/webp' })).toBe('agentos-flow-aaaaaaaa.webp')
  })

  it('falls back to png for a type it does not know', () => {
    expect(imageFileName({ ...IMG, mime: '' })).toBe('agentos-flow-aaaaaaaa.png')
  })
})

describe('savedFileNames', () => {
  it("pairs each image with its file's base name, on either path separator", () => {
    expect(
      savedFileNames({
        images: [IMG, OTHER],
        files: ['/state/outputs/fl/a_1.png', 'C:\\state\\outputs\\fl\\a_2.png'],
      }),
    ).toEqual({ [IMG.sha256]: 'a_1.png', [OTHER.sha256]: 'a_2.png' })
  })

  it('has nothing to say about nodes that wrote no files', () => {
    expect(savedFileNames(undefined)).toEqual({})
    expect(savedFileNames({ images: [IMG] })).toEqual({})
    expect(savedFileNames({ images: [IMG, OTHER], files: ['/x/a.png'] })).toEqual({
      [IMG.sha256]: 'a.png',
    })
  })
})

describe('runFileNames', () => {
  it("gathers every node's files, skipping nodes without any", () => {
    expect(
      runFileNames({
        image: { images: [IMG] },
        save1: { images: [IMG], files: ['/x/a.png'] },
        save2: { images: [OTHER], files: ['/x/b.png'] },
      }),
    ).toEqual({ [IMG.sha256]: 'a.png', [OTHER.sha256]: 'b.png' })
    expect(runFileNames({})).toEqual({})
  })
})

describe('base64ToBlob', () => {
  it('decodes to the original bytes', async () => {
    const blob = base64ToBlob(btoa('\x89PNG\xff'), 'image/png')
    expect(blob.type).toBe('image/png')
    const bytes = await new Promise<Uint8Array>((resolve, reject) => {
      const reader = new FileReader()
      reader.onerror = () => reject(reader.error ?? new Error('read failed'))
      reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer))
      reader.readAsArrayBuffer(blob)
    })
    expect([...bytes]).toEqual([0x89, 0x50, 0x4e, 0x47, 0xff])
  })
})
