import '../memory.css'
import { BookOpen, Brain, Layers, Search, Settings2, Sparkles } from 'lucide-react'
import type { ReactNode } from 'react'
import { t } from '~/i18n'
import { Head, Notice, Pill, Stat, Stats, Tabs, type Tone } from '../parts'
import { useConfigSnapshot } from '../use-snapshot'
import { useConnected, useDocuments, useFacts, useIndexStatus } from '../memory/api'
import { useDraftStore } from '../memory/drafts'
import { embeddingTitle } from '../memory/EmbeddingCard'
import { KnowledgeTab } from '../memory/KnowledgeTab'
import { memoryConfig, savedEmbeddingProvider, searchKind } from '../memory/logic'
import { RememberedTab } from '../memory/RememberedTab'
import { SetupTab } from '../memory/SetupTab'
import { useMemoryTab, type MemoryTab } from '../memory/tab-store'
import { TryTab } from '../memory/TryTab'
import { count, fill } from '../memory/ui'

/**
 * Settings › Memory: what the agent remembers (two lists you can edit), the
 * documents it can look things up in, a search to test what it would find,
 * and the setup of how memory is searched, recalled and learned. Every tab
 * stays mounted so an edit in one is never lost by visiting another; a
 * hidden tab is also inert, so the sheet's focus trap (which reads the
 * focusable controls by selector) does not take a hidden control for its
 * first or last stop and let Tab out of the sheet.
 */
export function MemoryPane() {
  const connected = useConnected()
  const tab = useMemoryTab((s) => s.tab)
  const setTab = useMemoryTab((s) => s.setTab)
  const unsaved = useDraftStore((s) => Object.keys(s.drafts).length > 0)
  const status = useIndexStatus()
  const work = useFacts('memory')
  const you = useFacts('user')
  const docs = useDocuments()
  const { snapshot } = useConfigSnapshot()

  const kind = status.data ? searchKind(status.data) : undefined
  const provider = snapshot ? savedEmbeddingProvider(snapshot) : undefined
  const deliberateKeywords =
    provider === 'none' || memoryConfig(snapshot?.config).retrieval_mode === 'fts_only'
  const sessionsIndexed = memoryConfig(snapshot?.config).session_source_enabled === true

  let pill: { tone?: Tone; text: string; pulse?: boolean }
  if (!connected) pill = { text: t('settings.memory.pill.offline') }
  else if (status.isError) pill = { tone: 'danger', text: t('settings.memory.pill.unavailable') }
  else if (!kind) pill = { text: t('settings.memory.pill.checking'), pulse: true }
  else if (kind === 'meaning') pill = { tone: 'ok', text: t('settings.memory.pill.meaning') }
  else if (kind === 'keywords')
    pill = {
      tone: deliberateKeywords ? undefined : 'warn',
      text: t('settings.memory.pill.keywords'),
    }
  else pill = { tone: 'danger', text: t('settings.memory.pill.unavailable') }

  const facts =
    work.data || you.data
      ? (work.data?.entries.length ?? 0) + (you.data?.entries.length ?? 0)
      : undefined

  const tabs: { value: MemoryTab; label: string; icon: ReactNode }[] = [
    { value: 'remembered', label: t('settings.memory.tab.remembered'), icon: <Brain /> },
    { value: 'knowledge', label: t('settings.memory.tab.knowledge'), icon: <BookOpen /> },
    { value: 'try', label: t('settings.memory.tab.try'), icon: <Search /> },
    {
      value: 'setup',
      label: t('settings.memory.tab.setup'),
      icon: unsaved ? (
        <span className="mem-tab-dot" role="img" aria-label={t('settings.memory.unsaved')} />
      ) : (
        <Settings2 />
      ),
    },
  ]

  return (
    <>
      <Head
        title={t('settings.section.memory')}
        blurb={t('settings.section.memory.blurb')}
        action={
          <Pill tone={pill.tone} pulse={pill.pulse}>
            {pill.text}
          </Pill>
        }
      />
      {!connected ? <Notice tone="info">{t('settings.offline')}</Notice> : null}

      <Stats label={t('settings.memory.stats')}>
        <Stat
          label={t('settings.memory.stat.facts')}
          value={count(facts)}
          hint={
            work.data && you.data
              ? fill('settings.memory.stat.facts.hint', {
                  work: work.data.entries.length,
                  you: you.data.entries.length,
                })
              : undefined
          }
          icon={<Brain />}
          onClick={() => setTab('remembered')}
        />
        <Stat
          label={t('settings.memory.stat.docs')}
          value={count(docs.data?.length)}
          hint={t('settings.memory.stat.docs.hint')}
          icon={<BookOpen />}
          onClick={() => setTab('knowledge')}
        />
        <Stat
          label={t('settings.memory.stat.chunks')}
          value={count(status.data?.chunkCount)}
          hint={
            status.data?.fileCount === undefined
              ? undefined
              : status.data.fileCount === 1
                ? t('settings.memory.stat.chunks.hint.one')
                : fill('settings.memory.stat.chunks.hint', { files: count(status.data.fileCount) })
          }
          icon={<Layers />}
        />
        <Stat
          label={t('settings.memory.stat.search')}
          value={
            kind === 'meaning'
              ? t('settings.memory.stat.search.meaning')
              : kind === 'keywords'
                ? t('settings.memory.stat.search.keywords')
                : t('settings.memory.dash')
          }
          tone={kind === 'keywords' && !deliberateKeywords ? 'warn' : undefined}
          hint={provider ? embeddingTitle(provider) : undefined}
          icon={<Sparkles />}
          onClick={() => setTab('setup')}
        />
      </Stats>

      <Tabs label={t('settings.memory.tabs')} value={tab} options={tabs} onChange={setTab} />

      <div
        role="tabpanel"
        className="mem-panel"
        aria-label={t('settings.memory.tab.remembered')}
        hidden={tab !== 'remembered'}
        inert={tab !== 'remembered'}
      >
        <RememberedTab active={tab === 'remembered'} />
      </div>
      <div
        role="tabpanel"
        className="mem-panel"
        aria-label={t('settings.memory.tab.knowledge')}
        hidden={tab !== 'knowledge'}
        inert={tab !== 'knowledge'}
      >
        <KnowledgeTab active={tab === 'knowledge'} />
      </div>
      <div
        role="tabpanel"
        className="mem-panel"
        aria-label={t('settings.memory.tab.try')}
        hidden={tab !== 'try'}
        inert={tab !== 'try'}
      >
        <TryTab kind={kind} sessionsIndexed={sessionsIndexed} />
      </div>
      <div
        role="tabpanel"
        className="mem-panel"
        aria-label={t('settings.memory.tab.setup')}
        hidden={tab !== 'setup'}
        inert={tab !== 'setup'}
      >
        <SetupTab active={tab === 'setup'} />
      </div>
    </>
  )
}
