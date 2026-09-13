// @vitest-environment node
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { locateCli } from './cli-locator'

let dir: string

function fakeBinary(where: string, name = 'agentos'): string {
  mkdirSync(where, { recursive: true })
  const file = path.join(where, name)
  writeFileSync(file, '#!/bin/sh\nexit 0\n')
  chmodSync(file, 0o755)
  return file
}

beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), 'agentos-cli-'))
})
afterEach(() => rmSync(dir, { recursive: true, force: true }))

describe('locateCli', () => {
  it('prefers an executable override', () => {
    const bin = fakeBinary(path.join(dir, 'custom'))
    expect(locateCli({ override: bin, envPath: '', fallbackDirs: [] })).toBe(bin)
  })

  it('ignores a non-executable override and searches PATH', () => {
    const bin = fakeBinary(path.join(dir, 'p'))
    expect(
      locateCli({
        override: path.join(dir, 'missing'),
        envPath: path.join(dir, 'p'),
        fallbackDirs: [],
      }),
    ).toBe(bin)
  })

  it('falls back to installer directories when PATH is empty', () => {
    const bin = fakeBinary(path.join(dir, 'fallback'))
    expect(locateCli({ envPath: '', fallbackDirs: [path.join(dir, 'fallback')] })).toBe(bin)
  })

  it('returns null when nothing matches', () => {
    expect(locateCli({ envPath: dir, fallbackDirs: [] })).toBeNull()
  })
})
