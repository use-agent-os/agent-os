#!/usr/bin/env node
// Import closures of the two consumers of frontend/src (overhaul-control-ui, D2).
//
// The desktop renderer imports part of the console through its `@/` alias, and
// the rebuilt web console boots from src/web/app. A file under frontend/src that
// neither of them reaches is legacy: once the cutover is done it is deleted,
// together with its tests. This script computes both closures and lists those
// orphans, so the deletion is computed rather than guessed.
//
//   node scripts/ui-closure.mjs            human-readable summary
//   node scripts/ui-closure.mjs --json     machine-readable report
//   node scripts/ui-closure.mjs --orphans  one orphan path per line

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
export const FRONTEND_DIR = path.resolve(HERE, '..')
export const REPO_DIR = path.resolve(FRONTEND_DIR, '..')
const FRONTEND_SRC = path.join(FRONTEND_DIR, 'src')
const DESKTOP_DIR = path.join(REPO_DIR, 'desktop')
const DESKTOP_SRC = path.join(DESKTOP_DIR, 'src')

// Same alias maps as frontend/vite.config.ts and desktop/electron.vite.config.ts.
const ALIASES = [
  ['@/', FRONTEND_SRC],
  ['~/', path.join(DESKTOP_SRC, 'renderer', 'src')],
  ['@shared/', path.join(DESKTOP_SRC, 'shared')],
]
const EXTENSIONS = ['.ts', '.tsx', '.js', '.jsx', '.mjs', '.json', '.css']
const SOURCE_SUFFIXES = ['.ts', '.tsx', '.js', '.jsx', '.mjs', '.css']
const SPECIFIER_PATTERNS = [
  // import x from '…' / export { x } from '…' / import type { T } from '…'
  /\b(?:import|export)\s[^'"`;]*?\sfrom\s*['"]([^'"]+)['"]/g,
  // import '…' (side effect)
  /\bimport\s*['"]([^'"]+)['"]/g,
  // import('…'), vi.mock('…'), vi.importActual('…'), require('…')
  /\b(?:import|mock|doMock|importActual|importOriginal|require)\(\s*['"]([^'"]+)['"]/g,
  // CSS @import and url() references (fonts, images)
  /@import\s+(?:url\()?\s*['"]([^'"]+)['"]/g,
  /\burl\(\s*['"]?([^'")]+?)['"]?\s*\)/g,
]

export function isTestFile(file) {
  return /\.test\.[cm]?[jt]sx?$/.test(file) || file.includes(`${path.sep}__fixtures__${path.sep}`)
}

function walk(dir, out = []) {
  if (!existsSync(dir)) return out
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name.startsWith('._')) continue
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) walk(full, out)
    else out.push(full)
  }
  return out
}

function isFile(candidate) {
  try {
    return statSync(candidate).isFile()
  } catch {
    return false
  }
}

function resolveSpecifier(specifier, fromFile) {
  const bare = specifier.replace(/[?#].*$/, '')
  let base = null
  if (bare.startsWith('.')) base = path.resolve(path.dirname(fromFile), bare)
  else {
    for (const [prefix, target] of ALIASES) {
      if (bare.startsWith(prefix)) {
        base = path.join(target, bare.slice(prefix.length))
        break
      }
    }
  }
  if (base === null) return null
  if (isFile(base)) return base
  for (const ext of EXTENSIONS) if (isFile(base + ext)) return base + ext
  for (const ext of EXTENSIONS) {
    const index = path.join(base, `index${ext}`)
    if (isFile(index)) return index
  }
  return null
}

const importCache = new Map()

export function importsOf(file) {
  const cached = importCache.get(file)
  if (cached) return cached
  const found = new Set()
  if (SOURCE_SUFFIXES.some((suffix) => file.endsWith(suffix))) {
    const text = readFileSync(file, 'utf8')
    for (const pattern of SPECIFIER_PATTERNS) {
      for (const match of text.matchAll(pattern)) {
        const resolved = resolveSpecifier(match[1], file)
        if (resolved) found.add(resolved)
      }
    }
  }
  const result = [...found]
  importCache.set(file, result)
  return result
}

export function closureOf(seeds) {
  const seen = new Set()
  const queue = [...seeds]
  while (queue.length > 0) {
    const file = queue.pop()
    if (seen.has(file)) continue
    seen.add(file)
    for (const next of importsOf(file)) if (!seen.has(next)) queue.push(next)
  }
  return seen
}

const rel = (file) => path.relative(FRONTEND_DIR, file).split(path.sep).join('/')

export function computeReport() {
  const desktopSeeds = walk(DESKTOP_SRC).filter((f) =>
    SOURCE_SUFFIXES.some((suffix) => f.endsWith(suffix)),
  )
  // Every module desktop names directly through `@/` — the contract desktop
  // depends on, as opposed to what those modules pull in transitively.
  const desktopDirect = new Set()
  for (const file of desktopSeeds) {
    for (const target of importsOf(file)) {
      if (target.startsWith(FRONTEND_SRC + path.sep)) desktopDirect.add(target)
    }
  }
  const desktop = closureOf(desktopSeeds)

  const webSeeds = [
    path.join(FRONTEND_SRC, 'main.tsx'),
    ...walk(path.join(FRONTEND_SRC, 'web', 'app')).filter((f) => !isTestFile(f)),
  ]
  const web = closureOf(webSeeds.filter(isFile))

  const frontendFiles = walk(FRONTEND_SRC)
  const isInfra = (f) =>
    f.endsWith('.d.ts') ||
    f.endsWith('.txt') ||
    f.startsWith(path.join(FRONTEND_SRC, 'test') + path.sep) ||
    path.basename(f) === '.gitkeep'
  const candidates = frontendFiles.filter((f) => !isTestFile(f) && !isInfra(f))
  const orphanSet = new Set(candidates.filter((f) => !desktop.has(f) && !web.has(f)))
  // A test goes with the module it covers: any test that imports an orphan.
  const orphanTests = frontendFiles
    .filter((f) => isTestFile(f) && /\.test\./.test(f))
    .filter((f) => importsOf(f).some((target) => orphanSet.has(target)))

  const inFrontend = (set) => [...set].filter((f) => f.startsWith(FRONTEND_SRC + path.sep))
  return {
    desktopDirect: [...desktopDirect].map(rel).sort(),
    desktopClosure: inFrontend(desktop).map(rel).sort(),
    webClosure: inFrontend(web).map(rel).sort(),
    orphans: [...orphanSet].map(rel).sort(),
    orphanTests: orphanTests.map(rel).sort(),
  }
}

function main(argv) {
  const started = performance.now()
  const report = computeReport()
  if (argv.includes('--json')) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`)
    return
  }
  if (argv.includes('--orphans')) {
    for (const file of report.orphans) process.stdout.write(`${file}\n`)
    return
  }
  const ms = Math.round(performance.now() - started)
  console.log(`Desktop imports ${report.desktopDirect.length} modules directly through @/`)
  console.log(`Desktop closure: ${report.desktopClosure.length} files under frontend/src`)
  console.log(`Web closure:     ${report.webClosure.length} files under frontend/src`)
  console.log(
    `Orphans:         ${report.orphans.length} files (+${report.orphanTests.length} tests)`,
  )
  for (const file of report.orphans) console.log(`  ${file}`)
  console.log(`Computed in ${ms} ms`)
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2))
}
