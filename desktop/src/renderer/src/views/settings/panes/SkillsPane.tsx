import { useQuery } from '@tanstack/react-query'
import { Sparkles } from 'lucide-react'
import { useRpc } from '@/app/providers'
import { skillStats, type RawSkill } from '@/views/skills/logic'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useConnection } from '@/stores/connection'
import { useUi } from '~/stores/ui'
import { Card, Head, Notice, Row, Value } from '../parts'

interface SkillsListResponse {
  skills?: RawSkill[]
}

/**
 * The Settings entry for skills is a summary and a door, not the library
 * itself: the library is a panel over the window (Skills in the sidebar,
 * ⌘⇧K), where lists, catalogs and install flows have the room they need.
 */
export function SkillsPane() {
  const rpc = useRpc()
  const connected = useConnection((s) => s.state === 'connected')
  const openSkills = useUi((s) => s.openSkills)

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

  return (
    <>
      <Head title={t('settings.section.skills')} blurb={t('settings.section.skills.blurb')} />
      {!connected ? <Notice tone="info">{t('settings.offline')}</Notice> : null}
      <Card
        title={t('settings.skills.library')}
        blurb={t('settings.skills.library.blurb')}
        action={
          <Button variant="primary" className="shrink-0 whitespace-nowrap" onClick={openSkills}>
            <Sparkles className="size-3.5" strokeWidth={2} aria-hidden />
            {t('settings.skills.open')}
          </Button>
        }
      >
        <Row label={t('settings.skills.installed')}>
          <Value>{connected && query.isSuccess ? String(stats.total) : '—'}</Value>
        </Row>
        <Row label={t('settings.skills.ready')}>
          <Value tone={connected && query.isSuccess && stats.ready > 0 ? 'ok' : undefined}>
            {connected && query.isSuccess ? String(stats.ready) : '—'}
          </Value>
        </Row>
        <Row label={t('settings.skills.needsSetup')} help={t('settings.skills.needsSetup.help')}>
          <Value tone={connected && query.isSuccess && stats.needs > 0 ? 'warn' : undefined}>
            {connected && query.isSuccess ? String(stats.needs) : '—'}
          </Value>
        </Row>
        {stats.disabled > 0 ? (
          <Row label={t('settings.skills.disabled')}>
            <Value>{String(stats.disabled)}</Value>
          </Row>
        ) : null}
        {computed ? (
          <Row label={t('settings.skills.offered')}>
            <Value>{`${offered} / ${stats.total}`}</Value>
          </Row>
        ) : null}
      </Card>
      <Card title={t('settings.skills.catalogs')} blurb={t('settings.skills.catalogs.blurb')}>
        <Row label={t('skills.source.robinhood.hint')}>
          <Value>Robinhood</Value>
        </Row>
        <Row label={t('skills.source.partner.hint')}>
          <Value>Bankr · Aeon · Capminal</Value>
        </Row>
        <Row label={t('skills.source.community.hint')}>
          <Value>{t('skills.source.community')}</Value>
        </Row>
      </Card>
    </>
  )
}
