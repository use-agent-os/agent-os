// Every catalog key the rebuilt web console uses exists (overhaul-control-ui
// D12). Type-checking already rejects most unknown literal keys; this also
// covers plural bases, keys picked by a ternary or kept in a lookup table
// (`titleKey: 'webShell.pageHome'`), and refuses keys built from a template,
// which no static check can follow.
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

/**
 * Every quoted `web*.key` literal, wherever it sits: inside a ternary passed
 * to `t()`, in a lookup table, or as a default. A literal names a key when its
 * namespace is a catalog namespace; it may be a plain or a plural key.
 */
function missingLiteralKeys(text: string): string[] {
  const missing: string[] = []
  for (const m of text.matchAll(/(['"`])(web\w*)\.(\w+)\1/g)) {
    const [, , ns, key] = m as unknown as [string, string, string, string]
    const messages = CATALOG[ns]
    if (!messages) continue
    const present =
      messages[key] !== undefined ||
      (messages[`${key}_one`] !== undefined && messages[`${key}_other`] !== undefined)
    if (!present) missing.push(`${ns}.${key}`)
  }
  return missing
}

/** `t()` / `tPlural()` keys built from a template: they cannot be checked. */
function templateKeys(text: string): string[] {
  return [...text.matchAll(/\b(?:t|tPlural)\(\s*`[^`]*\$\{[^`]*`/g)].map((m) => m[0])
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

  it('resolves keys chosen by a ternary or kept in a lookup table', () => {
    const problems = sources().flatMap(({ file, text }) =>
      missingLiteralKeys(text).map((key) => `${file}: ${key}`),
    )
    expect(problems).toEqual([])
  })

  it('reports a missing key hidden in a ternary or a table', () => {
    expect(missingLiteralKeys("t(on ? 'webCommon.retry' : 'webCommon.noSuchKey')")).toEqual([
      'webCommon.noSuchKey',
    ])
    expect(missingLiteralKeys("const KEYS = { a: 'webCommon.alsoMissing' }")).toEqual([
      'webCommon.alsoMissing',
    ])
    // A plural base is a key too; a non-catalog namespace is not a key at all.
    expect(missingLiteralKeys("'webPagesWork.sessionCount' 'example.com'")).toEqual([])
  })

  it('builds no t() / tPlural() key from a template', () => {
    const problems = sources().flatMap(({ file, text }) =>
      templateKeys(text).map((call) => `${file}: ${call}`),
    )
    expect(problems).toEqual([])
    expect(templateKeys('t(`webCommon.${name}`)')).toHaveLength(1)
    expect(templateKeys('t(`webCommon.retry`)')).toEqual([])
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
