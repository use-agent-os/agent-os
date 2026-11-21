import { useQuery } from '@tanstack/react-query'
import { Globe, Sparkles } from 'lucide-react'
import { useRpc } from '@/app/providers'
import { skillStats, type RawSkill } from '@/views/skills/logic'
import { Button } from '~/components/ui/button'
import { t, type MessageKey } from '~/i18n'
import { useConnection } from '@/stores/connection'
import { useUi } from '~/stores/ui'
import { PARTNER_LABEL, type PartnerBrand } from '~/views/skills/logic'
import { BrandMark } from '~/views/skills/SkillMark'
import { Card, Head, Notice, Stat, Stats } from '../parts'
import { useArrivalFocus } from './focus'

interface SkillsListResponse {
  skills?: RawSkill[]
}

/** Where skills come from, in the order the Skills panel lists them. */
const SOURCES: readonly { brand: PartnerBrand | 'community'; hint: MessageKey }[] = [
  { brand: 'robinhood', hint: 'skills.source.robinhood.hint' },
  { brand: 'bankr', hint: 'skills.source.partner.hint' },
  { brand: 'aeon', hint: 'skills.source.partner.hint' },
  { brand: 'capminal', hint: 'skills.source.partner.hint' },
  { brand: 'community', hint: 'skills.source.community.hint' },
]

function fill(key: MessageKey, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce(
    (text, [name, value]) => text.replace(`{${name}}`, String(value)),
    t(key),
  )
}

/**
 * The Settings entry for skills is a summary and a door, not the library
 * itself: the library is a panel over the window (Skills in the sidebar,
 * ⌘⇧K), where lists, catalogs and install flows have the room they need.
 */
export function SkillsPane() {
  useArrivalFocus('skills')
  const rpc = useRpc()
  const connected = useConnection((s) => s.state === 'connected')
  const openSkills = useUi((s) => s.openSkills)
  const openNeedingSetup = useUi((s) => s.openSkillsNeedingSetup)

  const query = useQuery({
    queryKey: ['skills'],
    enabled: connected,
    refetchOnWindowFocus: false,
    queryFn: async () => {
      const data = await rpc.call<SkillsListResponse>('skills.list', {})
      return data.skills ?? []
    },
  })
  const skills = query.data ?? []
  const stats = skillStats(skills)
  const offered = skills.filter((s) => s.availability?.offered === true).length
  const computed = skills.some((s) => typeof s.availability?.offered === 'boolean')
  const known = connected && query.isSuccess

  return (
    <>
      <Head
        title={t('settings.section.skills')}
        blurb={t('settings.section.skills.blurb')}
        action={
          <Button variant="primary" onClick={openSkills}>
            <Sparkles className="size-3.5" strokeWidth={2} aria-hidden />
            {t('settings.skills.open')}
          </Button>
        }
      />
      {!connected ? <Notice tone="info">{t('settings.offline')}</Notice> : null}
      {query.isError ? <Notice tone="danger">{t('settings.skills.loadError')}</Notice> : null}

      <Stats label={t('settings.skills.stats')}>
        <Stat
          label={t('settings.skills.installed')}
          value={known ? stats.total : '—'}
          hint={
            known && stats.disabled > 0
              ? fill('settings.skills.disabled.hint', { n: stats.disabled })
              : undefined
          }
        />
        <Stat
          label={t('settings.skills.ready')}
          value={known ? stats.ready : '—'}
          tone={known && stats.ready > 0 ? 'ok' : undefined}
        />
        <Stat
          label={t('settings.skills.needsSetup')}
          hint={known && stats.needs > 0 ? t('settings.skills.needsSetup.help') : undefined}
          value={known ? stats.needs : '—'}
          tone={known && stats.needs > 0 ? 'warn' : undefined}
          onClick={known && stats.needs > 0 ? openNeedingSetup : undefined}
        />
        {computed ? (
          <Stat
            label={t('settings.skills.offered')}
            hint={t('settings.skills.offered.help')}
            value={`${offered} / ${stats.total}`}
          />
        ) : null}
      </Stats>

      <Card title={t('settings.skills.catalogs')} blurb={t('settings.skills.catalogs.blurb')}>
        <ul className="skl-sources" aria-label={t('settings.skills.catalogs')}>
          {SOURCES.map((source) => (
            <li key={source.brand} className="skl-source">
              {source.brand === 'community' ? (
                <span className="skl-source__glyph" aria-hidden>
                  <Globe strokeWidth={1.75} />
                </span>
              ) : (
                <BrandMark brand={source.brand} size="sm" />
              )}
              <span className="skl-source__text">
                <span className="skl-source__name">
                  {source.brand === 'community'
                    ? t('skills.source.community')
                    : PARTNER_LABEL[source.brand]}
                </span>
                <span className="skl-source__hint">{t(source.hint)}</span>
              </span>
            </li>
          ))}
        </ul>
      </Card>
    </>
  )
}
