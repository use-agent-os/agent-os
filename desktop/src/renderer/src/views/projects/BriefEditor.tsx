import { AlertCircle, Check } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { t } from '~/i18n'
import { briefStats, formatCount, isDirty, KNOWLEDGE_MAX_CHARS, type SaveState } from './logic'

/** Quiet period after the last keystroke before the brief saves itself. */
export const AUTOSAVE_MS = 900

/**
 * The brief as a page of text, not a field: no box, no Save button in the
 * way. It saves itself a moment after you stop typing, when focus leaves,
 * on ⌘S, and when the page is left with edits pending. The footer is the
 * only chrome: save state on the left, size on the right.
 */
export function BriefEditor({
  projectId,
  saved,
  onSave,
}: {
  projectId: string
  /** The brief as the gateway holds it. */
  saved: string
  /** Persist the text; resolves when the gateway has it. */
  onSave: (text: string) => Promise<unknown>
}) {
  const [draft, setDraft] = useState(saved)
  const [state, setState] = useState<SaveState>('clean')
  const ref = useRef<HTMLTextAreaElement>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // The timer, blur and unmount paths read the latest of these without being
  // re-bound on every keystroke.
  const onSaveRef = useRef(onSave)
  const draftRef = useRef(draft)
  const savedRef = useRef(saved)
  const inFlight = useRef<string | null>(null)
  useEffect(() => {
    onSaveRef.current = onSave
  }, [onSave])
  useEffect(() => {
    draftRef.current = draft
  }, [draft])

  // Another client (or our own save) moved the server copy. Follow it only
  // when there is nothing unsaved here; otherwise the next save will either
  // land or come back as a conflict, and the conflict handler reloads.
  useEffect(() => {
    const prev = savedRef.current
    savedRef.current = saved
    if (saved === prev) return
    if (draftRef.current === saved) {
      // Our own save landed.
      setState('clean')
      return
    }
    if (!isDirty(draftRef.current, prev)) {
      setDraft(saved)
      setState('clean')
    }
  }, [saved])

  const tooLong = draft.length > KNOWLEDGE_MAX_CHARS
  const dirty = isDirty(draft, saved)

  const flush = useCallback(async () => {
    if (timer.current) {
      clearTimeout(timer.current)
      timer.current = null
    }
    const text = draftRef.current
    if (!isDirty(text, savedRef.current)) return
    if (text.length > KNOWLEDGE_MAX_CHARS) return
    if (inFlight.current === text) return
    inFlight.current = text
    setState('saving')
    try {
      await onSaveRef.current(text)
      // Only report saved if nothing else was typed meanwhile.
      if (draftRef.current === text) setState('saved')
      else setState('dirty')
    } catch {
      setState('error')
    } finally {
      if (inFlight.current === text) inFlight.current = null
    }
  }, [])

  function schedule() {
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => void flush(), AUTOSAVE_MS)
  }

  // Leaving the page with edits pending still saves them.
  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current)
      const text = draftRef.current
      if (isDirty(text, savedRef.current) && text.length <= KNOWLEDGE_MAX_CHARS) {
        void onSaveRef.current(text)
      }
    }
  }, [projectId])

  const stats = briefStats(draft)
  const shownState: SaveState = dirty && state !== 'saving' && state !== 'error' ? 'dirty' : state

  return (
    <section className="proj-brief" aria-labelledby="proj-brief-title" data-state={shownState}>
      <div className="proj-brief__head">
        <h2 id="proj-brief-title">{t('projects.brief.title')}</h2>
        <p>{t('projects.brief.hint')}</p>
      </div>
      <textarea
        ref={ref}
        className="proj-brief__text app-no-drag"
        value={draft}
        placeholder={t('projects.brief.placeholder')}
        aria-label={t('projects.brief.title')}
        aria-invalid={tooLong || undefined}
        spellCheck
        onChange={(e) => {
          setDraft(e.target.value)
          setState('dirty')
          schedule()
        }}
        onBlur={() => void flush()}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
            e.preventDefault()
            void flush()
          }
        }}
      />
      <footer className="proj-brief__foot">
        <span className="proj-brief__state" data-state={shownState} aria-live="polite">
          {shownState === 'saved' || shownState === 'clean' ? (
            <Check className="size-3" strokeWidth={2.5} aria-hidden />
          ) : shownState === 'error' ? (
            <AlertCircle className="size-3" strokeWidth={2} aria-hidden />
          ) : (
            <span className="proj-brief__pulse" aria-hidden />
          )}
          {t(`projects.brief.state.${shownState}`)}
          {shownState === 'error' ? (
            <button type="button" className="proj-brief__retry" onClick={() => void flush()}>
              {t('projects.brief.save')}
            </button>
          ) : null}
        </span>
        <span className="proj-brief__size" data-over={tooLong}>
          {tooLong ? <span>{t('projects.brief.tooLong')}</span> : null}
          <span>
            {formatCount(stats.words)} {t('projects.brief.words')}
          </span>
          <span className="proj-brief__chars">
            {formatCount(stats.chars)} / {formatCount(KNOWLEDGE_MAX_CHARS)}
          </span>
        </span>
      </footer>
    </section>
  )
}
