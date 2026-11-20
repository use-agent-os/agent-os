import { Brain, Pencil, Plus, Search, Trash2, UserRound } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { toast } from 'sonner'
import { filterCuratedEntries, isNearBudget } from '@/views/memory/logic'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { Card, Empty, Meter, Notice } from '../parts'
import { errorText, useConnected, useFacts, useMemoryAction } from './api'
import { AGENT_ID, factsTotalAfter } from './logic'
import { Confirm, Spinner, count, fill } from './ui'

type Target = 'memory' | 'user'

/** Above this many facts a filter field appears over the list. */
const FILTER_AT = 8

const COPY = {
  memory: {
    title: 'settings.memory.work.title',
    blurb: 'settings.memory.work.blurb',
    empty: 'settings.memory.empty.work',
    placeholder: 'settings.memory.editor.placeholder.work',
    file: 'MEMORY.md',
  },
  user: {
    title: 'settings.memory.you.title',
    blurb: 'settings.memory.you.blurb',
    empty: 'settings.memory.empty.you',
    placeholder: 'settings.memory.editor.placeholder.you',
    file: 'USER.md',
  },
} as const

/** The two lists the agent always has in mind: about your work, about you. */
export function RememberedTab() {
  return (
    <>
      <FactsCard target="memory" />
      <FactsCard target="user" />
    </>
  )
}

type Editing = { mode: 'add' } | { mode: 'edit'; entry: string } | null

function FactsCard({ target }: { target: Target }) {
  const copy = COPY[target]
  const connected = useConnected()
  const query = useFacts(target)
  const action = useMemoryAction()
  const [editing, setEditing] = useState<Editing>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const filterId = useId()
  const data = query.data
  const entries = data?.entries ?? []
  const blocked = !connected || Boolean(data?.loadFailed)
  const shown = entries.length > FILTER_AT ? filterCuratedEntries(entries, filter) : entries

  async function save(text: string) {
    const content = text.trim()
    if (!content || !editing) return
    const done = await action.run('save', async () => {
      if (editing.mode === 'edit') {
        await action.rpc.call('memory.curated.replace', {
          agentId: AGENT_ID,
          target,
          oldText: editing.entry,
          newContent: content,
        })
      } else {
        await action.rpc.call('memory.curated.add', { agentId: AGENT_ID, target, content })
      }
      return true
    })
    if (done) {
      toast.success(t('settings.memory.saved'), { id: `stg-memory-fact-${target}` })
      setEditing(null)
    }
  }

  async function remove(entry: string) {
    const done = await action.run('remove', async () => {
      await action.rpc.call('memory.curated.remove', { agentId: AGENT_ID, target, oldText: entry })
      return true
    })
    if (done) toast.success(t('settings.memory.removed'), { id: `stg-memory-fact-${target}` })
    setRemoving(null)
  }

  const used = data?.charCount ?? 0
  const limit = data?.charLimit ?? 0
  const full = limit > 0 && used >= limit
  const near = !full && isNearBudget(used, limit)

  return (
    <Card
      title={t(copy.title)}
      blurb={
        <>
          {t(copy.blurb)} <span className="mem-file">{copy.file}</span>
        </>
      }
      icon={
        <span className="mem-card-icon" aria-hidden>
          {target === 'memory' ? <Brain /> : <UserRound />}
        </span>
      }
      action={
        entries.length > 0 || editing ? (
          <Button
            disabled={blocked || editing !== null}
            aria-label={fill('settings.memory.addTo', { list: t(copy.title) })}
            onClick={() => setEditing({ mode: 'add' })}
          >
            <Plus className="size-3.5" strokeWidth={2} aria-hidden />
            {t('settings.memory.add')}
          </Button>
        ) : undefined
      }
    >
      <div className="mem-facts">
        {!connected ? (
          <p className="mem-quiet">{t('settings.memory.offlineList')}</p>
        ) : query.isPending ? (
          <Spinner />
        ) : query.isError ? (
          <Notice
            tone="danger"
            action={
              <Button onClick={() => void query.refetch()}>{t('settings.memory.retry')}</Button>
            }
          >
            {t('settings.memory.curatedFailed')} {errorText(query.error)}
          </Notice>
        ) : (
          <>
            {limit > 0 && entries.length > 0 ? (
              <Meter
                label={t('settings.memory.space')}
                value={used}
                max={limit}
                caption={fill('settings.memory.space.caption', {
                  used: count(used),
                  limit: count(limit),
                })}
              />
            ) : null}
            {data?.loadFailed ? (
              <Notice tone="danger">
                {fill('settings.memory.loadFailed', { file: copy.file })}
              </Notice>
            ) : full ? (
              <Notice tone="danger">{t('settings.memory.full')}</Notice>
            ) : near ? (
              <Notice>{t('settings.memory.nearFull')}</Notice>
            ) : null}

            {entries.length > FILTER_AT ? (
              <label className="mac-search mem-filter" htmlFor={filterId}>
                <Search className="size-3.5" strokeWidth={2} aria-hidden />
                <input
                  id={filterId}
                  type="search"
                  className="app-no-drag"
                  value={filter}
                  placeholder={t('settings.memory.filter')}
                  aria-label={t('settings.memory.filter')}
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(e) => setFilter(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape' && filter) {
                      e.stopPropagation()
                      setFilter('')
                    }
                  }}
                />
              </label>
            ) : null}

            {editing?.mode === 'add' ? (
              <FactEditor
                placeholder={t(copy.placeholder)}
                initial=""
                entries={entries}
                limit={limit}
                busy={action.busy === 'save'}
                onCancel={() => setEditing(null)}
                onSave={(text) => void save(text)}
              />
            ) : null}

            {entries.length === 0 && editing === null ? (
              <Empty
                icon={target === 'memory' ? <Brain /> : <UserRound />}
                title={t('settings.memory.empty.title')}
                body={t(copy.empty)}
                action={
                  <Button
                    variant="primary"
                    disabled={blocked}
                    onClick={() => setEditing({ mode: 'add' })}
                  >
                    <Plus className="size-3.5" strokeWidth={2} aria-hidden />
                    {t('settings.memory.addFact')}
                  </Button>
                }
              />
            ) : shown.length === 0 && filter ? (
              <p className="mem-quiet">{fill('settings.memory.filter.none', { query: filter })}</p>
            ) : shown.length > 0 ? (
              <ul className="mem-fact-list" aria-label={t(copy.title)}>
                {shown.map((entry, i) =>
                  editing?.mode === 'edit' && editing.entry === entry ? (
                    <li key={`${i}:${entry}`} className="mem-fact" data-editing="true">
                      <FactEditor
                        placeholder={t(copy.placeholder)}
                        initial={entry}
                        replacing={entry}
                        entries={entries}
                        limit={limit}
                        busy={action.busy === 'save'}
                        onCancel={() => setEditing(null)}
                        onSave={(text) => void save(text)}
                      />
                    </li>
                  ) : (
                    <li key={`${i}:${entry}`} className="mem-fact">
                      <p className="mem-fact__text">{entry}</p>
                      <span className="mem-fact__actions">
                        <Button
                          variant="ghost"
                          size="icon"
                          disabled={blocked || editing !== null}
                          aria-label={fill('settings.memory.editNamed', { fact: clip(entry) })}
                          title={t('settings.memory.edit')}
                          onClick={() => setEditing({ mode: 'edit', entry })}
                        >
                          <Pencil className="size-3.5" strokeWidth={1.75} aria-hidden />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="mem-danger-icon"
                          disabled={blocked || editing !== null}
                          aria-label={fill('settings.memory.removeNamed', { fact: clip(entry) })}
                          title={t('settings.memory.remove')}
                          onClick={() => setRemoving(entry)}
                        >
                          <Trash2 className="size-3.5" strokeWidth={1.75} aria-hidden />
                        </Button>
                      </span>
                    </li>
                  ),
                )}
              </ul>
            ) : null}
          </>
        )}
      </div>

      {removing !== null ? (
        <Confirm
          title={t('settings.memory.confirmRemove.title')}
          body={<blockquote className="mem-quote">{removing}</blockquote>}
          confirm={t('settings.memory.confirmRemove.confirm')}
          busy={action.busy === 'remove'}
          onCancel={() => setRemoving(null)}
          onConfirm={() => void remove(removing)}
        />
      ) : null}
    </Card>
  )
}

function clip(text: string): string {
  const one = text.replace(/\s+/g, ' ').trim()
  return one.length > 48 ? `${one.slice(0, 47)}…` : one
}

/** Add or edit one fact in place: a textarea with its own budget line. */
function FactEditor({
  initial,
  placeholder,
  replacing,
  entries,
  limit,
  busy,
  onCancel,
  onSave,
}: {
  initial: string
  placeholder: string
  replacing?: string
  entries: readonly string[]
  limit: number
  busy: boolean
  onCancel: () => void
  onSave: (text: string) => void
}) {
  const [text, setText] = useState(initial)
  const ref = useRef<HTMLTextAreaElement>(null)
  const id = useId()
  const hintId = useId()

  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
  }, [])

  const total = text.trim() ? factsTotalAfter(entries, text, replacing) : 0
  const over = limit > 0 ? total - limit : 0
  const unchanged = text.trim() === initial.trim()
  const canSave = !busy && text.trim() !== '' && !unchanged && over <= 0

  return (
    <div className="mem-editor">
      <label htmlFor={id} className="sr-only">
        {t('settings.memory.editor.label')}
      </label>
      <textarea
        ref={ref}
        id={id}
        className="mac-textarea"
        rows={3}
        value={text}
        placeholder={placeholder}
        aria-describedby={hintId}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.preventDefault()
            e.stopPropagation()
            onCancel()
          } else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault()
            if (canSave) onSave(text)
          }
        }}
      />
      <div className="mem-editor__foot">
        <span id={hintId} className="mem-editor__hint" data-tone={over > 0 ? 'danger' : undefined}>
          {over > 0
            ? fill('settings.memory.editor.over', { over: count(over) })
            : limit > 0 && text.trim()
              ? fill('settings.memory.editor.left', { left: count(limit - total) })
              : t('settings.memory.editor.hint')}
        </span>
        <Button disabled={busy} onClick={onCancel}>
          {t('settings.memory.cancel')}
        </Button>
        <Button variant="primary" disabled={!canSave} onClick={() => onSave(text)}>
          {t('settings.memory.saveFact')}
        </Button>
      </div>
    </div>
  )
}
