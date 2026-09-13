import { net } from 'electron'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { readdir, stat } from 'node:fs/promises'
import path from 'node:path'
import { isPetSlug, petSheetUrl, type InstalledPet, type PetManifestEntry } from '@shared/pet'

const MANIFEST_URL = 'https://petdex.dev/api/manifest'
const MANIFEST_TTL_MS = 5 * 60_000
const FETCH_TIMEOUT_MS = 30_000
/** Only these hosts may serve a sheet: the manifest is public JSON, so a
 *  URL in it is not trusted to point anywhere. */
const ASSET_HOSTS = new Set(['assets.petdex.dev', 'petdex.dev'])

interface RawManifestEntry {
  slug?: unknown
  displayName?: unknown
  kind?: unknown
  submittedBy?: unknown
  spritesheetUrl?: unknown
  petJsonUrl?: unknown
  spriteVersionNumber?: unknown
}

interface ManifestRow extends PetManifestEntry {
  spritesheetUrl: string
  petJsonUrl: string
}

export class PetStoreError extends Error {}

/**
 * Pets on disk, petdex layout: `<pets>/<slug>/pet.json` + `spritesheet.webp`.
 * Gallery previews of pets that are not installed go to `<pets>/.cache/`
 * so browsing never pollutes the installed list. Everything the renderer
 * shows comes back through the `agentos-pet://` protocol (protocol.ts).
 */
export class PetStore {
  private manifest: { at: number; rows: ManifestRow[] } | null = null
  private inflight = new Map<string, Promise<string>>()

  constructor(readonly root: string) {}

  private dir(slug: string): string {
    return path.join(this.root, slug)
  }
  private cacheDir(): string {
    return path.join(this.root, '.cache')
  }

  /** The file the protocol serves for `agentos-pet://sheet/<slug>`, or null. */
  sheetPath(slug: string): string | null {
    if (!isPetSlug(slug)) return null
    const installed = path.join(this.dir(slug), 'spritesheet.webp')
    if (existsSync(installed)) return installed
    const cached = path.join(this.cacheDir(), `${slug}.webp`)
    return existsSync(cached) ? cached : null
  }

  async installed(): Promise<InstalledPet[]> {
    let names: string[] = []
    try {
      names = await readdir(this.root)
    } catch {
      return []
    }
    const pets: InstalledPet[] = []
    for (const slug of names.sort()) {
      if (!isPetSlug(slug)) continue
      const dir = this.dir(slug)
      try {
        if (!(await stat(dir)).isDirectory()) continue
        if (!existsSync(path.join(dir, 'spritesheet.webp'))) continue
        const meta = readJson(path.join(dir, 'pet.json'))
        pets.push({
          slug,
          displayName: String(meta.displayName || slug),
          description: String(meta.description || ''),
          sheetUrl: petSheetUrl(slug),
        })
      } catch {
        /* not a pet folder */
      }
    }
    return pets
  }

  async fetchManifest(force = false): Promise<PetManifestEntry[]> {
    const rows = await this.rows(force)
    return rows.map((row) => ({
      slug: row.slug,
      displayName: row.displayName,
      kind: row.kind,
      submittedBy: row.submittedBy,
      spriteVersion: row.spriteVersion,
    }))
  }

  private async rows(force = false): Promise<ManifestRow[]> {
    if (!force && this.manifest && Date.now() - this.manifest.at < MANIFEST_TTL_MS) {
      return this.manifest.rows
    }
    const onDisk = path.join(this.root, '.manifest.json')
    try {
      const res = await fetchWithTimeout(MANIFEST_URL)
      const payload = (await res.json()) as { pets?: unknown }
      const rows = parseManifest(payload)
      this.manifest = { at: Date.now(), rows }
      mkdirSync(this.root, { recursive: true })
      writeFileSync(onDisk, JSON.stringify(payload), 'utf8')
      return rows
    } catch (err) {
      // Offline: the last manifest we saw still lets the gallery open.
      try {
        const rows = parseManifest(JSON.parse(readFileSync(onDisk, 'utf8')))
        this.manifest = { at: Date.now(), rows }
        return rows
      } catch {
        throw new PetStoreError(`Could not reach petdex.dev: ${errorText(err)}`)
      }
    }
  }

  private async entry(slug: string): Promise<ManifestRow> {
    const rows = await this.rows()
    const row = rows.find((r) => r.slug === slug)
    if (!row) throw new PetStoreError(`No pet named "${slug}" on petdex.dev.`)
    return row
  }

  /** Download the sheet into the cache so a gallery tile can show it. */
  async preview(slug: string): Promise<string> {
    if (!isPetSlug(slug)) throw new PetStoreError('Not a pet slug.')
    if (this.sheetPath(slug)) return petSheetUrl(slug)
    const running = this.inflight.get(slug)
    if (running) return running
    const job = (async () => {
      const row = await this.entry(slug)
      mkdirSync(this.cacheDir(), { recursive: true })
      await downloadTo(row.spritesheetUrl, path.join(this.cacheDir(), `${slug}.webp`))
      return petSheetUrl(slug)
    })().finally(() => this.inflight.delete(slug))
    this.inflight.set(slug, job)
    return job
  }

  async install(slug: string): Promise<InstalledPet> {
    if (!isPetSlug(slug)) throw new PetStoreError('Not a pet slug.')
    const dir = this.dir(slug)
    const sheet = path.join(dir, 'spritesheet.webp')
    if (!existsSync(sheet)) {
      const row = await this.entry(slug)
      mkdirSync(dir, { recursive: true })
      const cached = path.join(this.cacheDir(), `${slug}.webp`)
      if (existsSync(cached)) renameSync(cached, sheet)
      else await downloadTo(row.spritesheetUrl, sheet)
      let meta: Record<string, unknown> = {}
      try {
        const res = await fetchWithTimeout(assertAssetUrl(row.petJsonUrl))
        meta = (await res.json()) as Record<string, unknown>
      } catch {
        /* the manifest row is enough to describe the pet */
      }
      const petJson = {
        id: slug,
        displayName: String(meta.displayName || row.displayName || slug),
        description: String(meta.description || ''),
        spriteVersionNumber: row.spriteVersion,
        spritesheetPath: 'spritesheet.webp',
      }
      writeFileSync(path.join(dir, 'pet.json'), JSON.stringify(petJson, null, 2) + '\n', 'utf8')
    }
    const meta = readJson(path.join(dir, 'pet.json'))
    return {
      slug,
      displayName: String(meta.displayName || slug),
      description: String(meta.description || ''),
      sheetUrl: petSheetUrl(slug),
    }
  }

  async remove(slug: string): Promise<void> {
    if (!isPetSlug(slug)) return
    rmSync(this.dir(slug), { recursive: true, force: true })
  }
}

function readJson(file: string): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(readFileSync(file, 'utf8'))
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

function parseManifest(payload: unknown): ManifestRow[] {
  const pets = (payload as { pets?: unknown })?.pets
  if (!Array.isArray(pets)) throw new PetStoreError('petdex manifest had no pets array')
  const rows: ManifestRow[] = []
  for (const raw of pets as RawManifestEntry[]) {
    const slug = typeof raw?.slug === 'string' ? raw.slug.trim() : ''
    const sheet = typeof raw?.spritesheetUrl === 'string' ? raw.spritesheetUrl : ''
    if (!isPetSlug(slug) || !isAssetUrl(sheet)) continue
    rows.push({
      slug,
      displayName: String(raw.displayName || slug),
      kind: String(raw.kind || 'pet'),
      submittedBy: String(raw.submittedBy || ''),
      spriteVersion: Number(raw.spriteVersionNumber) || 1,
      spritesheetUrl: sheet,
      petJsonUrl: typeof raw.petJsonUrl === 'string' ? raw.petJsonUrl : '',
    })
  }
  return rows
}

function isAssetUrl(url: string): boolean {
  try {
    const u = new URL(url)
    return u.protocol === 'https:' && ASSET_HOSTS.has(u.hostname)
  } catch {
    return false
  }
}

function assertAssetUrl(url: string): string {
  if (!isAssetUrl(url)) throw new PetStoreError(`Refusing to fetch ${url}: not a petdex asset.`)
  return url
}

async function fetchWithTimeout(url: string): Promise<Response> {
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS)
  try {
    const res = await net.fetch(url, {
      signal: ctl.signal,
      headers: { 'User-Agent': 'agentos-desktop-petdex' },
    })
    if (!res.ok) throw new PetStoreError(`${url}: HTTP ${res.status}`)
    return res
  } finally {
    clearTimeout(timer)
  }
}

/** Download to a temp file, then rename: a half-written sheet is never served. */
async function downloadTo(url: string, dest: string): Promise<void> {
  const res = await fetchWithTimeout(assertAssetUrl(url))
  const bytes = Buffer.from(await res.arrayBuffer())
  if (bytes.length === 0) throw new PetStoreError(`${url}: empty download`)
  const tmp = `${dest}.part`
  writeFileSync(tmp, bytes)
  renameSync(tmp, dest)
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}
