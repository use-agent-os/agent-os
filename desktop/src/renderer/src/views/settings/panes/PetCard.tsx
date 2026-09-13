import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Search } from 'lucide-react'
import { useId, useState } from 'react'
import { toast } from 'sonner'
import type { InstalledPet, PetManifestEntry } from '@shared/pet'
import { PET_FRAME_W, PET_MAX_SCALE, PET_MIN_SCALE } from '@shared/pet'
import { Button } from '~/components/ui/button'
import { Switch } from '~/components/ui/switch'
import { t } from '~/i18n'
import { desktopApi, isDesktop } from '~/lib/desktop-api'
import { cn } from '~/lib/utils'
import { useSettings } from '~/stores/settings'
import { Card, Notice, Row } from '../parts'
import '~/components/pet/pet.css'

/** How many gallery tiles to show at once; type to narrow it. */
const PAGE = 36
const INSTALLED_KEY = ['pets', 'installed'] as const
const MANIFEST_KEY = ['pets', 'manifest'] as const

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export function filterPets(
  installed: InstalledPet[],
  manifest: PetManifestEntry[],
  query: string,
  limit = PAGE,
): { slug: string; name: string; installed: boolean; kind?: string }[] {
  const q = query.trim().toLowerCase()
  const seen = new Set<string>()
  const out: { slug: string; name: string; installed: boolean; kind?: string }[] = []
  const matches = (slug: string, name: string) =>
    !q || slug.includes(q) || name.toLowerCase().includes(q)
  for (const p of installed) {
    if (!matches(p.slug, p.displayName)) continue
    seen.add(p.slug)
    out.push({ slug: p.slug, name: p.displayName, installed: true })
  }
  for (const p of manifest) {
    if (out.length >= limit) break
    if (seen.has(p.slug) || !matches(p.slug, p.displayName)) continue
    seen.add(p.slug)
    out.push({ slug: p.slug, name: p.displayName, installed: false, kind: p.kind })
  }
  return out
}

/**
 * Settings > Appearance > Pet: the toggle, the petdex gallery (installed
 * pets first, then the public manifest, a page at a time) and the size
 * slider. Picking a pet installs it if needed and makes it the active one.
 */
export function PetCard() {
  const pet = useSettings((s) => s.settings.pet)
  const update = useSettings((s) => s.update)
  const queryClient = useQueryClient()
  const desktop = isDesktop()
  const [query, setQuery] = useState('')
  const sliderId = useId()

  const installed = useQuery({
    queryKey: INSTALLED_KEY,
    enabled: desktop,
    queryFn: () => desktopApi().pets.installed(),
  })
  const manifest = useQuery({
    queryKey: MANIFEST_KEY,
    enabled: desktop,
    staleTime: 5 * 60_000,
    retry: 1,
    queryFn: () => desktopApi().pets.manifest(),
  })

  const pick = useMutation({
    mutationFn: (slug: string) => desktopApi().pets.install(slug),
    onSuccess: async (installedPet) => {
      await update({ pet: { slug: installedPet.slug, enabled: true } })
      await queryClient.invalidateQueries({ queryKey: INSTALLED_KEY })
      toast.success(`${installedPet.displayName} ${t('settings.pet.adopted')}`, { id: 'stg-pet' })
    },
    onError: (err) =>
      toast.error(`${t('settings.pet.installFailed')}: ${errorText(err)}`, { id: 'stg-pet-err' }),
  })

  const tiles = filterPets(installed.data ?? [], manifest.data ?? [], query)
  const total = (manifest.data?.length ?? 0) + (installed.data?.length ?? 0)

  return (
    <Card
      title={t('settings.pet.title')}
      blurb={t('settings.pet.blurb')}
      action={
        <Switch
          checked={pet.enabled}
          disabled={!desktop}
          aria-label={t('settings.pet.enabled')}
          onCheckedChange={(enabled) => void update({ pet: { enabled } })}
        />
      }
    >
      {!desktop ? (
        <div className="stg-card__body">
          <Notice tone="info">{t('settings.pet.desktopOnly')}</Notice>
        </div>
      ) : (
        <>
          <Row label={t('settings.pet.choose')} help={t('settings.pet.choose.help')} stack>
            <label className="mac-search app-no-drag" style={{ width: '100%' }}>
              <Search className="size-3.5 shrink-0" strokeWidth={1.75} aria-hidden />
              <input
                type="search"
                placeholder={t('settings.pet.search')}
                aria-label={t('settings.pet.search')}
                autoComplete="off"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
          </Row>
          <div className="stg-card__body">
            {manifest.isError && !installed.data?.length ? (
              <Notice tone="info">{t('settings.pet.offline')}</Notice>
            ) : null}
            <div role="radiogroup" aria-label={t('settings.pet.choose')} className="pet-grid">
              {tiles.map((tile) => (
                <button
                  key={tile.slug}
                  type="button"
                  role="radio"
                  aria-checked={tile.slug === pet.slug}
                  className="pet-tile app-no-drag"
                  disabled={pick.isPending}
                  onClick={() => {
                    if (tile.slug === pet.slug) return
                    if (tile.installed) void update({ pet: { slug: tile.slug, enabled: true } })
                    else pick.mutate(tile.slug)
                  }}
                >
                  <PetThumb slug={tile.slug} />
                  <span className="pet-tile__text">
                    <span className="pet-tile__name">{tile.name}</span>
                    <span className="pet-tile__meta">
                      {tile.slug}
                      {tile.installed ? ` · ${t('settings.pet.installed')}` : ''}
                    </span>
                  </span>
                  {tile.slug === pet.slug ? (
                    <Check className="pet-tile__check size-3.5" strokeWidth={2.5} aria-hidden />
                  ) : null}
                </button>
              ))}
            </div>
            <p className="pet-grid__foot">
              {manifest.isPending
                ? t('settings.pet.loading')
                : `${t('settings.pet.showing')} ${tiles.length} ${t('settings.pet.of')} ${total}. ${t('settings.pet.narrow')}`}
            </p>
          </div>
          <Row label={t('settings.pet.size')} help={t('settings.pet.size.help')} htmlFor={sliderId}>
            <span className="stg-slider">
              <input
                id={sliderId}
                type="range"
                min={PET_MIN_SCALE * 100}
                max={PET_MAX_SCALE * 100}
                step={5}
                value={Math.round(pet.scale * 100)}
                onChange={(e) => void update({ pet: { scale: Number(e.target.value) / 100 } })}
              />
              <output htmlFor={sliderId}>{Math.round(pet.scale * 100)}%</output>
            </span>
          </Row>
          {pet.slug ? (
            <Row label={t('settings.pet.current')}>
              <span className="stg-value">{pet.slug}</span>
              <Button
                onClick={async () => {
                  await desktopApi().pets.remove(pet.slug!)
                  await update({ pet: { slug: null, enabled: false } })
                  await queryClient.invalidateQueries({ queryKey: INSTALLED_KEY })
                }}
              >
                {t('settings.pet.remove')}
              </Button>
            </Row>
          ) : null}
        </>
      )}
    </Card>
  )
}

/** First idle frame of the sheet, fetched (and cached by main) on first sight. */
function PetThumb({ slug }: { slug: string }) {
  const preview = useQuery({
    queryKey: ['pets', 'preview', slug],
    staleTime: Infinity,
    retry: false,
    queryFn: () => desktopApi().pets.preview(slug),
  })
  const [cols, setCols] = useState(8)
  const url = preview.data ?? null
  const scale = 48 / PET_FRAME_W
  return (
    <span className={cn('pet-thumb', !url && 'pet-thumb--empty')} aria-hidden>
      {url ? (
        <img
          src={url}
          alt=""
          draggable={false}
          style={{ width: cols * PET_FRAME_W * scale, height: 'auto' }}
          onLoad={(e) =>
            setCols(Math.max(1, Math.round(e.currentTarget.naturalWidth / PET_FRAME_W)))
          }
        />
      ) : null}
    </span>
  )
}
