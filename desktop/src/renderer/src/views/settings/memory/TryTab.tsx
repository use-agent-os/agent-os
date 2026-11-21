import { useQuery } from '@tanstack/react-query'
import { History, Search, SearchX, Sparkles } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { useRpc } from '@/app/providers'
import {
  formatScore,
  type MemorySearchResultWire,
  type MemorySourceFilter,
} from '@/views/memory/logic'
import { Button } from '~/components/ui/button'
import { t, type MessageKey } from '~/i18n'
import { Card, Empty, Notice, Pill, Segmented } from '../parts'
import { errorText, MEMORY_KEY, useConnected, useRecallStatus } from './api'
import {
  AGENT_ID,
  documentName,
  recallTone,
  relativeTime,
  type RecallRecordWire,
  type SearchKind,
} from './logic'
import { Spinner, counted, fill } from './ui'

/** The engine's own default: the agent's `memory_search` drops weaker matches. */
const MIN_SCORE = 0.35
const LIMIT = 10

const SOURCE_LABEL: Record<string, MessageKey> = {
  memory: 'settings.memory.source.memory',
  knowledge_base: 'settings.memory.source.knowledge_base',
  sessions: 'settings.memory.source.sessions',
}

/** A snippet without blank lines or the lists' bare § separators. */
function tidySnippet(text: string): string {
  return text
    .split('\n')
    .filter((line) => line.trim() !== '' && line.trim() !== '§')
    .join('\n')
}

/** Search the way the agent does, then see what recall added to recent replies. */
export function TryTab({
  kind,
  sessionsIndexed,
}: {
  kind: SearchKind | undefined
  sessionsIndexed: boolean
}) {
  return (
    <>
      <SearchCard kind={kind} sessionsIndexed={sessionsIndexed} />
      <RecentRecallCard />
    </>
  )
}

function SearchCard({
  kind,
  sessionsIndexed,
}: {
  kind: SearchKind | undefined
  sessionsIndexed: boolean
}) {
  const rpc = useRpc()
  const connected = useConnected()
  const [text, setText] = useState('')
  const [query, setQuery] = useState('')
  const [source, setSource] = useState<MemorySourceFilter>('all')
  const [weak, setWeak] = useState(false)
  const inputId = useId()

  useEffect(() => {
    const id = window.setTimeout(() => setQuery(text.trim()), 300)
    return () => window.clearTimeout(id)
  }, [text])

  const results = useQuery({
    queryKey: [...MEMORY_KEY, 'search', query, source, weak],
    enabled: connected && query.length > 0,
    retry: false,
    queryFn: async () => {
      const res = await rpc.call<{ results?: MemorySearchResultWire[] }>('memory.search', {
        agentId: AGENT_ID,
        query,
        source,
        minScore: weak ? 0 : MIN_SCORE,
        limit: LIMIT,
      })
      return res?.results ?? []
    },
  })

  const settling = text.trim() !== query
  const rows = results.data ?? []

  return (
    <Card
      title={t('settings.memory.try.title')}
      blurb={t('settings.memory.try.blurb')}
      icon={
        <span className="mem-card-icon" aria-hidden>
          <Sparkles />
        </span>
      }
    >
      <div className="mem-try">
        <label className="mem-search" htmlFor={inputId}>
          <Search className="size-4" strokeWidth={1.75} aria-hidden />
          <input
            id={inputId}
            type="search"
            className="app-no-drag"
            value={text}
            disabled={!connected}
            placeholder={t('settings.memory.try.placeholder')}
            aria-label={t('settings.memory.try.label')}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape' && text) {
                e.stopPropagation()
                setText('')
              }
            }}
          />
        </label>
        <div className="mem-try__filters">
          <Segmented
            label={t('settings.memory.try.source')}
            value={source}
            disabled={!connected}
            options={[
              { value: 'all', label: t('settings.memory.try.source.all') },
              { value: 'memory', label: t('settings.memory.try.source.memory') },
              { value: 'knowledge_base', label: t('settings.memory.try.source.knowledge') },
              { value: 'sessions', label: t('settings.memory.try.source.sessions') },
            ]}
            onChange={setSource}
          />
          <label className="mem-check">
            <input
              type="checkbox"
              checked={weak}
              disabled={!connected}
              onChange={(e) => setWeak(e.target.checked)}
            />
            {t('settings.memory.try.weak')}
          </label>
        </div>
        {source === 'all' || source === 'memory' ? (
          <p className="mem-fine">{t('settings.memory.try.userNote')}</p>
        ) : null}
        {source === 'sessions' && !sessionsIndexed ? (
          <Notice tone="info">
            {t('settings.memory.try.sessionsOff')}{' '}
            <code className="mem-key">memory.session_source_enabled</code>
          </Notice>
        ) : null}

        {!connected ? (
          <p className="mem-quiet">{t('settings.memory.offlineList')}</p>
        ) : !query ? (
          <Empty
            icon={<Search />}
            title={t('settings.memory.try.idle.title')}
            body={t('settings.memory.try.idle.body')}
          />
        ) : results.isError ? (
          <Notice
            tone="danger"
            action={
              <Button onClick={() => void results.refetch()}>{t('settings.memory.retry')}</Button>
            }
          >
            {t('settings.memory.try.failed')} {errorText(results.error)}
          </Notice>
        ) : results.isPending || (settling && rows.length === 0) ? (
          <Spinner label={t('settings.memory.try.searching')} />
        ) : rows.length === 0 ? (
          <Empty
            icon={<SearchX />}
            title={fill('settings.memory.try.none.title', { query })}
            body={
              kind === 'keywords'
                ? `${t('settings.memory.try.none.body')} ${t('settings.memory.try.none.keywords')}`
                : t('settings.memory.try.none.body')
            }
          />
        ) : (
          <>
            <p className="mem-try__count" role="status">
              {rows.length === 1
                ? t('settings.memory.try.results.one')
                : fill('settings.memory.try.results.many', { count: rows.length })}
            </p>
            <ol className="mem-results" aria-label={t('settings.memory.try.resultsLabel')}>
              {rows.map((r) => (
                <li key={r.chunkId || `${r.path}:${r.startLine}`} className="mem-result">
                  <div className="mem-result__head">
                    <span className="mem-result__path" title={r.path}>
                      {documentName(r.path)}
                    </span>
                    {r.startLine ? (
                      <span className="mem-result__lines">
                        {r.endLine && r.endLine !== r.startLine
                          ? fill('settings.memory.try.lines', {
                              start: r.startLine,
                              end: r.endLine,
                            })
                          : fill('settings.memory.try.line', { line: r.startLine })}
                      </span>
                    ) : null}
                    <Pill>{SOURCE_LABEL[r.source] ? t(SOURCE_LABEL[r.source]!) : r.source}</Pill>
                    <span
                      className="mem-score"
                      aria-label={fill('settings.memory.try.score', {
                        score: formatScore(r.score),
                      })}
                    >
                      <span className="mem-score__bar" aria-hidden>
                        <span style={{ width: `${Math.round(Math.min(1, r.score) * 100)}%` }} />
                      </span>
                      {formatScore(r.score)}
                    </span>
                  </div>
                  <p className="mem-result__snippet">{tidySnippet(r.snippet)}</p>
                </li>
              ))}
            </ol>
          </>
        )}
      </div>
    </Card>
  )
}

const STATUS_KEY: Record<string, MessageKey> = {
  injected: 'settings.memory.recall.status.injected',
  none: 'settings.memory.recall.status.none',
  skipped: 'settings.memory.recall.status.skipped',
  unavailable: 'settings.memory.recall.status.unavailable',
  error: 'settings.memory.recall.status.error',
}

const DECISION_KEY: Record<string, MessageKey> = {
  recall: 'settings.memory.recall.decision.deep',
  'strong-trigger-hit': 'settings.memory.recall.decision.quickHit',
  'no-recall-intent': 'settings.memory.recall.decision.quick',
  'mode-off': 'settings.memory.recall.decision.quick',
}

function when(at: number, now: number): string {
  const r = relativeTime(at, now)
  if (r.unit === 'now') return t('settings.memory.time.now')
  return fill(`settings.memory.time.${r.unit}`, { n: r.n })
}

function RecentRecallCard() {
  const connected = useConnected()
  const status = useRecallStatus()
  const records = (status.data?.recent ?? []).filter((r): r is RecallRecordWire => r !== null)
  const enabled = status.data?.config?.enabled !== false
  // Ages are counted from when the list was read; it is read again every 30 s.
  const now = status.dataUpdatedAt
  return (
    <Card
      title={t('settings.memory.recall.title')}
      blurb={t('settings.memory.recall.blurb')}
      icon={
        <span className="mem-card-icon" aria-hidden>
          <History />
        </span>
      }
    >
      <div className="mem-docs">
        {!connected ? (
          <p className="mem-quiet">{t('settings.memory.offlineList')}</p>
        ) : status.isPending ? (
          <Spinner />
        ) : status.isError ? (
          <p className="mem-quiet">
            {t('settings.memory.recall.failed')} {errorText(status.error)}
          </p>
        ) : !enabled ? (
          <p className="mem-quiet">{t('settings.memory.recall.off')}</p>
        ) : records.length === 0 ? (
          <p className="mem-quiet">{t('settings.memory.recall.empty')}</p>
        ) : (
          <ul className="mem-recall" aria-label={t('settings.memory.recall.title')}>
            {records.map((r) => {
              const hits = (r.triggerHits ?? 0) + (r.deepHits ?? 0)
              const decision = r.decision ? DECISION_KEY[r.decision] : undefined
              return (
                <li key={`${r.sessionKey}:${r.at}`} className="mem-recall__row">
                  <Pill tone={recallTone(r.status)}>
                    {STATUS_KEY[r.status]
                      ? fill(STATUS_KEY[r.status]!, {
                          chars: counted(r.injectedChars ?? 0, 'char'),
                        })
                      : r.status}
                  </Pill>
                  <span className="mem-recall__session" title={r.sessionKey}>
                    {r.sessionKey}
                  </span>
                  <span className="mem-recall__meta">
                    {[
                      decision ? t(decision) : null,
                      hits > 0 ? fill('settings.memory.recall.hits', { hits }) : null,
                      r.elapsedMs ? fill('settings.memory.recall.ms', { ms: r.elapsedMs }) : null,
                      when(r.at, now),
                    ]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </Card>
  )
}
