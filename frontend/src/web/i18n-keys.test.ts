// Every catalog key the rebuilt web console uses exists (overhaul-control-ui
// D12). Type-checking already rejects most unknown literal keys; this also
// covers plural bases and keeps the rule explicit for the `web*` namespaces.
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { en } from '@/i18n/en'

const WEB_DIR = 'src/web'
const CATALOG = en as unknown as Record<string, Record<string, string>>

function sources(): { file: string; text: string }[] {
  return readdirSync(WEB_DIR, { recursive: true, withFileTypes: true })
    .filter((e) => e.isFile() && /\.tsx?$/.test(e.name) && !/\.test\.tsx?$/.test(e.name))
    .map((e) => path.join(e.parentPath, e.name))
    .map((file) => ({ file, text: readFileSync(file, 'utf8') }))
}

function missingKeys(text: string): string[] {
  const missing: string[] = []
  for (const m of text.matchAll(/\b(t|tPlural)\(\s*'(\w+)\.(\w+)'/g)) {
    const [, fn, ns, key] = m as unknown as [string, string, string, string]
    const messages = CATALOG[ns]
    const present =
      fn === 'tPlural'
        ? messages?.[`${key}_one`] !== undefined && messages?.[`${key}_other`] !== undefined
        : messages?.[key] !== undefined
    if (!present) missing.push(`${ns}.${key}`)
  }
  return missing
}

describe('web catalog keys', () => {
  it('resolves every t() / tPlural() key used under src/web', () => {
    const problems = sources().flatMap(({ file, text }) =>
      missingKeys(text).map((key) => `${file}: ${key}`),
    )
    expect(problems).toEqual([])
  })

  it('reports a key that is missing from its catalog', () => {
    expect(missingKeys("t('webCommon.retry'); t('webCommon.noSuchKey')")).toEqual([
      'webCommon.noSuchKey',
    ])
    expect(missingKeys("tPlural('webCommon.retry', 2)")).toEqual(['webCommon.retry'])
  })

  it('keeps web copy in web-prefixed namespaces', () => {
    const used = new Set(
      sources().flatMap(({ text }) =>
        [...text.matchAll(/\bt(?:Plural)?\(\s*'(\w+)\./g)].map((m) => m[1]!),
      ),
    )
    const foreign = [...used].filter((ns) => !ns.startsWith('web'))
    expect(foreign).toEqual([])
  })
})
