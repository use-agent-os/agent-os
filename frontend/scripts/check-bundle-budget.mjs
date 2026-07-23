import { readFile, readdir } from 'node:fs/promises'
import { dirname, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { gzipSync } from 'node:zlib'

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const distDir = resolve(frontendDir, '../src/agentos/gateway/static/dist')
const assetsDir = resolve(distDir, 'assets')
const indexPath = resolve(distDir, 'index.html')

const initialBudgets = {
  js: 180 * 1024,
  css: 25 * 1024,
}
// A route-only dependency can still make its first navigation feel frozen.
// Keep every individual lazy chunk bounded as well as the initial shell.
const chunkBudgets = {
  js: 180 * 1024,
  css: 25 * 1024,
}

async function assetFiles(directory) {
  const files = []
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name)
    if (entry.isDirectory()) files.push(...(await assetFiles(path)))
    else if (entry.isFile() && /\.(?:js|css)$/.test(entry.name)) files.push(path)
  }
  return files
}

const html = await readFile(indexPath, 'utf8')
const assetRefs = [
  ...html.matchAll(/(?:src|href)=["']([^"'?#]+\.(?:js|css))(?:[?#][^"']*)?["']/g),
].map((match) => match[1])

const totals = { js: 0, css: 0 }
const measured = []

for (const ref of new Set(assetRefs)) {
  const path = resolve(distDir, ref.replace(/^\.\//, ''))
  if (path !== distDir && !path.startsWith(`${distDir}${sep}`)) {
    throw new Error(`Refusing to measure asset outside dist: ${ref}`)
  }

  const contents = await readFile(path)
  const type = ref.endsWith('.css') ? 'css' : 'js'
  const gzipBytes = gzipSync(contents, { level: 9 }).byteLength
  totals[type] += gzipBytes
  measured.push({ ref, gzipBytes })
}

if (!measured.some(({ ref }) => ref.endsWith('.js'))) {
  throw new Error(`No initial JavaScript asset found in ${indexPath}`)
}

for (const type of ['js', 'css']) {
  const actualKiB = (totals[type] / 1024).toFixed(1)
  const budgetKiB = (initialBudgets[type] / 1024).toFixed(0)
  console.log(`Initial ${type.toUpperCase()}: ${actualKiB} KiB gzip / ${budgetKiB} KiB budget`)
  if (totals[type] > initialBudgets[type]) {
    process.exitCode = 1
  }
}

const chunkMeasurements = await Promise.all(
  (await assetFiles(assetsDir)).map(async (path) => {
    const type = path.endsWith('.css') ? 'css' : 'js'
    const gzipBytes = gzipSync(await readFile(path), { level: 9 }).byteLength
    return { ref: relative(distDir, path), type, gzipBytes }
  }),
)

for (const type of ['js', 'css']) {
  const largest = chunkMeasurements
    .filter((entry) => entry.type === type)
    .sort((left, right) => right.gzipBytes - left.gzipBytes)[0]
  if (!largest) throw new Error(`No ${type.toUpperCase()} chunks found in ${assetsDir}`)

  const actualKiB = (largest.gzipBytes / 1024).toFixed(1)
  const budgetKiB = (chunkBudgets[type] / 1024).toFixed(0)
  console.log(
    `Largest ${type.toUpperCase()} chunk: ${actualKiB} KiB gzip / ${budgetKiB} KiB budget (${largest.ref})`,
  )
  if (largest.gzipBytes > chunkBudgets[type]) process.exitCode = 1
}

if (process.exitCode) {
  console.error(
    'Bundle budget exceeded. Keep route-only code behind route.lazy(), import focused modules, or adjust the budget with measured justification.',
  )
}
