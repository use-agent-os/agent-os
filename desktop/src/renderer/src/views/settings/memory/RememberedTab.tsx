import { Brain, Pencil, Plus, Search, Trash2, UserRound } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { toast } from 'sonner'
import { filterCuratedEntries, isNearBudget } from '@/views/memory/logic'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { Card, Empty, Meter, Notice } from '../parts'
import {
  MEMORY_KEY,
  errorText,
  useConnected,
  useFacts,
  useMemoryAction,
  useRefreshWhenShown,
} from './api'
import { factRowButton, refocus, rowButtonAt } from './focus'
import { AGENT_ID, factFailure, factRoom, separatorProblem } from './logic'
import { useMemoryTab } from './tab-store'
import { Confirm, Spinner, count, counted, fill } from './ui'

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
export function RememberedTab({ active }: { active: boolean }) {
  useRefreshWhenShown(active, [[...MEMORY_KEY, 'curated']])
  return (
    <>
      <FactsCard target="memory" />
      <FactsCard target="user" />
    </>
  )
}

/** What the editor is for; `text` lives here so a refreshed list never drops it. */
type Editing = { mode: 'add'; text: string } | { mode: 'edit'; entry: string; text: string } | null

function failureText(error: unknown, file: string): string {
  const message = errorText(error)
  switch (factFailure(message)) {
    case 'stale':
      return t('settings.memory.fact.stale')
    case 'ambiguous':
      return t('settings.memory.fact.ambiguous')
    case 'full':
      return t('settings.memory.fact.full')
    case 'damaged':
      return fill('settings.memory.fact.damaged', { file })
    case 'unreadable':
      return fill('settings.memory.fact.unreadable', { file })
    case 'split':
      return t('settings.memory.editor.split')
    default:
      return fill('settings.memory.fact.failed', { error: message })
  }
}

function FactsCard({ target }: { target: Target }) {
  const copy = COPY[target]
  const connected = useConnected()
  const query = useFacts(target)
  const action = useMemoryAction()
  const setTab = useMemoryTab((s) => s.setTab)
  const [editing, setEditing] = useState<Editing>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const filterId = useId()
  const box = useRef<HTMLDivElement>(null)
  const data = query.data
  const entries = data?.entries ?? []
  const blocked = !connected || Boolean(data?.loadFailed)
  const shown = entries.length > FILTER_AT ? filterCuratedEntries(entries, filter) : entries
  // The fact being edited is no longer in the list (the agent changed it):
  // keep the text, offer it as a new fact instead.
  const gone = editing?.mode === 'edit' && data !== undefined && !entries.includes(editing.entry)
  const adding = editing?.mode === 'add' || gone
  const describe = (error: unknown) => failureText(error, copy.file)

  async function save() {
    if (!editing) return
    const content = editing.text.trim()
    if (!content) return
    const done = await action.run(
      'save',
      async () => {
        if (editing.mode === 'edit' && !gone) {
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
      },
      describe,
    )
    if (done) {
      toast.success(t('settings.memory.saved'), { id: `stg-memory-fact-${target}` })
      setEditing(null)
      // The editor goes: land on the saved fact's row.
      refocus(box.current, () => factRowButton(box.current, content) ?? addButton())
    }
  }

  /** Close the editor and go back to where it was opened from. */
  function cancel() {
    const from = editing
    setEditing(null)
    refocus(box.current, () =>
      from?.mode === 'edit' ? (factRowButton(box.current, from.entry) ?? addButton()) : addButton(),
    )
  }

  /** The list's Add button: the header one, or the empty state's. */
  function addButton(): HTMLElement | null {
    const card = box.current?.closest('section')
    return (
      card?.querySelector<HTMLElement>(`button[aria-label="${CSS.escape(addLabel)}"]`) ??
      box.current?.querySelector<HTMLElement>('.stg-empty button') ??
      null
    )
  }

  async function remove(entry: string) {
    const at = shown.indexOf(entry)
    const done = await action.run(
      'remove',
      async () => {
        await action.rpc.call('memory.curated.remove', {
          agentId: AGENT_ID,
          target,
          oldText: entry,
        })
        return true
      },
      describe,
    )
    if (done) toast.success(t('settings.memory.removed'), { id: `stg-memory-fact-${target}` })
    setRemoving(null)
    // The row (or, on failure, the list as it was) goes: land on the next
    // row, else the one before, else Add.
    refocus(box.current, () =>
      done
        ? (rowButtonAt(box.current, 'li.mem-fact', Math.max(0, at)) ?? addButton())
        : (factRowButton(box.current, entry) ??
          rowButtonAt(box.current, 'li.mem-fact', 0) ??
          addButton()),
    )
  }

  const addLabel = fill('settings.memory.addTo', { list: t(copy.title) })
  const used = data?.charCount ?? 0
  const limit = data?.charLimit ?? 0
  // Full means not even a one-character fact fits after the break before it.
  const full = limit > 0 && entries.length > 0 && factRoom(entries, limit) < 1
  const near = !full && isNearBudget(used, limit)
  const setText = (text: string) => setEditing((e) => (e ? { ...e, text } : e))

  const editor = (initial: string, replacing?: string) =>
    editing ? (
      <FactEditor
        placeholder={t(copy.placeholder)}
        text={editing.text}
        initial={initial}
        room={limit > 0 ? factRoom(entries, limit, replacing) : null}
        breakCounted={replacing === undefined ? entries.length > 0 : entries.length > 1}
        notice={gone ? t('settings.memory.editor.gone') : undefined}
        saveLabel={gone ? t('settings.memory.saveAsNew') : undefined}
        busy={action.busy === 'save'}
        onText={setText}
        onCancel={cancel}
        onSave={() => void save()}
      />
    ) : null

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
            disabled={blocked || editing !== null || full}
            aria-label={addLabel}
            title={full ? t('settings.memory.full.add') : undefined}
            onClick={() => setEditing({ mode: 'add', text: '' })}
          >
            <Plus className="size-3.5" strokeWidth={2} aria-hidden />
            {t('settings.memory.add')}
          </Button>
        ) : undefined
      }
    >
      <div className="mem-facts" ref={box}>
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
              <Notice
                tone="danger"
                action={
                  <Button
                    onClick={() => setTab('setup', target === 'user' ? 'limit-you' : 'limit-work')}
                  >
                    {t('settings.memory.full.raise')}
                  </Button>
                }
              >
                {t('settings.memory.full')}
              </Notice>
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

            {adding && editing ? editor(editing.mode === 'edit' ? editing.entry : '') : null}

            {entries.length === 0 && editing === null ? (
              <Empty
                icon={target === 'memory' ? <Brain /> : <UserRound />}
                title={t('settings.memory.empty.title')}
                body={t(copy.empty)}
                action={
                  <Button
                    variant="primary"
                    disabled={blocked}
                    onClick={() => setEditing({ mode: 'add', text: '' })}
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
                  editing?.mode === 'edit' && !gone && editing.entry === entry ? (
                    <li key={`${i}:${entry}`} className="mem-fact" data-editing="true">
                      {editor(entry, entry)}
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
                          onClick={() => setEditing({ mode: 'edit', entry, text: entry })}
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
  text,
  initial,
  placeholder,
  room,
  breakCounted,
  notice,
  saveLabel,
  busy,
  onText,
  onCancel,
  onSave,
}: {
  text: string
  initial: string
  placeholder: string
  /** Characters this fact may take, or null when the limit is unknown. */
  room: number | null
  /** The list has other facts, so a break of three characters goes before this one. */
  breakCounted: boolean
  notice?: string
  saveLabel?: string
  busy: boolean
  onText: (text: string) => void
  onCancel: () => void
  onSave: () => void
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  const id = useId()
  const hintId = useId()

  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
  }, [])

  const content = text.trim()
  const separator = separatorProblem(text)
  const split = separator !== null
  const over = room !== null && content ? content.length - room : 0
  const unchanged = content === initial.trim()
  const canSave = !busy && content !== '' && !unchanged && over <= 0 && !split

  let hint: string
  if (separator === 'bare') hint = t('settings.memory.editor.bare')
  else if (separator === 'line') hint = t('settings.memory.editor.split')
  else if (over > 0)
    hint = fill(
      breakCounted ? 'settings.memory.editor.over.break' : 'settings.memory.editor.over',
      {
        over: counted(over, 'char'),
        room: counted(Math.max(0, room ?? 0), 'char'),
      },
    )
  else if (room !== null && content)
    hint = fill('settings.memory.editor.left', { left: counted(room - content.length, 'char') })
  else if (room !== null)
    hint = fill('settings.memory.editor.room', { room: counted(room, 'char') })
  else hint = t('settings.memory.editor.hint')

  return (
    <div className="mem-editor">
      {notice ? <Notice tone="warn">{notice}</Notice> : null}
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
        aria-invalid={split || over > 0 || undefined}
        disabled={busy}
        onChange={(e) => onText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.preventDefault()
            e.stopPropagation()
            onCancel()
          } else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault()
            if (canSave) onSave()
          }
        }}
      />
      <div className="mem-editor__foot">
        <span
          id={hintId}
          className="mem-editor__hint"
          data-tone={split || over > 0 ? 'danger' : undefined}
        >
          {hint}
        </span>
        <Button disabled={busy} onClick={onCancel}>
          {t('settings.memory.cancel')}
        </Button>
        <Button variant="primary" disabled={!canSave} onClick={onSave}>
          {saveLabel ?? t('settings.memory.saveFact')}
        </Button>
      </div>
    </div>
  )
}
