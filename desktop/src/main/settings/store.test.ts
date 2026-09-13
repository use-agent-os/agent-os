// @vitest-environment node
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { DEFAULT_SETTINGS } from '@shared/settings'
import { SettingsStore } from './store'

let dir: string
let file: string

beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), 'agentos-desktop-'))
  file = path.join(dir, 'nested', 'settings.json')
})

afterEach(() => rmSync(dir, { recursive: true, force: true }))

describe('SettingsStore', () => {
  it('falls back to defaults when the file is missing', () => {
    expect(new SettingsStore(file).get()).toEqual(DEFAULT_SETTINGS)
  })

  it('falls back to defaults when the file is corrupt', () => {
    writeFileSync(path.join(dir, 'settings.json'), '{not json', 'utf8')
    expect(new SettingsStore(path.join(dir, 'settings.json')).get()).toEqual(DEFAULT_SETTINGS)
  })

  it('persists a patch and re-reads it', () => {
    const store = new SettingsStore(file)
    store.update({ theme: { preference: 'dark' } })
    const reread = new SettingsStore(file).get()
    expect(reread.theme.preference).toBe('dark')
    expect(reread.theme.palette).toBe(DEFAULT_SETTINGS.theme.palette)
    expect(JSON.parse(readFileSync(file, 'utf8')).theme.preference).toBe('dark')
  })

  it('normalizes invalid values inside a patch', () => {
    const store = new SettingsStore(file)
    const next = store.update({
      theme: { preference: 'neon' as never },
      gateway: { port: 99999 },
    })
    expect(next.theme.preference).toBe(DEFAULT_SETTINGS.theme.preference)
    expect(next.gateway.port).toBe(DEFAULT_SETTINGS.gateway.port)
  })

  it('notifies subscribers with a detached copy', () => {
    const store = new SettingsStore(file)
    const seen: string[] = []
    const off = store.subscribe((s) => seen.push(s.theme.palette))
    store.update({ theme: { palette: 'graphite' } })
    off()
    store.update({ theme: { palette: 'tactical' } })
    expect(seen).toEqual(['graphite'])
  })
})
