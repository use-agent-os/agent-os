import './logs.css'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { DownloadIcon, ScrollTextIcon, SearchIcon } from 'lucide-react'
import { toast } from 'sonner'
import { AsciiField } from '@/components/AsciiField'
import { Button } from '@/components/ui/button'
import { useRpc } from '@/app/providers'
import {
  DEFAULT_LEVELS,
  LEVELS,
  buildExportText,
  clampBuffer,
  countByLevel,
  extractLines,
  filterLines,
  normalizeEntry,
  sliceTs,
  splitHighlight,
  type Level,
  type LogLine,
} from './logic'

// logs.js:262-290 — the logs.status payload shape (all optional; the view
// tolerates a missing/partial status and falls back to a warn pill).
interface LogsStatus {
  gateway_file_log?: { enabled?: boolean; path?: string }
  raw_turn_call_log?: {
    enabled?: boolean
    source?: string
    directory?: { path?: string }
  }
  diagnostics_enabled?: { effective?: boolean; detail?: string }
}

const POLL_MS = 3000 // logs.js:135
const TAIL_LIMIT = 500 // logs.js:180

// logs.js:113-120 — per-level tone for error/warn lines. Only error/warn carry
// a severity gutter (the --tone primitive); info/debug/trace stay neutral so
// there is never more than one colored bar per line.
const LEVEL_TONE: Partial<Record<Level, 'danger' | 'warn'>> = {
  ERROR: 'danger',
  WARN: 'warn',
}

// logs.js:262-290 — one status pill.
function StatusPill({ label, tone }: { label: string; tone?: 'warn' }) {
  return <span className={`lg-pill${tone === 'warn' ? ' tone-warn' : ''}`}>{label}</span>
}

function StatusPills({ status }: { status: LogsStatus | null }) {
  if (!status) {
    return (
      <div className="lg-status-pills" aria-label="Log status">
        <StatusPill label="Log status unavailable" tone="warn" />
      </div>
    )
  }
  const fileLog = status.gateway_file_log || {}
  const rawLog = status.raw_turn_call_log || {}
  const diagnostics = status.diagnostics_enabled || {}
  const diagnosticsLabel =
    diagnostics.detail === 'raw'
      ? 'Diagnostics raw'
      : diagnostics.effective
        ? 'Diagnostics standard'
        : 'Diagnostics off'
  return (
    <div className="lg-status-pills" aria-label="Log status">
      <StatusPill
        label={`File log ${fileLog.enabled ? 'on' : 'off'}`}
        tone={fileLog.enabled ? undefined : 'warn'}
      />
      <StatusPill
        label={`Raw turn-call ${rawLog.enabled ? 'on' : 'off'}`}
        tone={rawLog.enabled ? undefined : 'warn'}
      />
      <StatusPill label={diagnosticsLabel} tone="warn" />
    </div>
  )
}

// logs.js:311-320 — one rendered log line: mono, ts + level chip + message.
function LogLineRow({ line, search }: { line: LogLine; search: string }) {
  const level = line.level
  const lvl = level.toLowerCase()
  const tone = LEVEL_TONE[level]
  const segments = splitHighlight(line.message, search)
  const ts = sliceTs(line.ts)
  return (
    <div className={`lg-line lg-line--${lvl}${tone ? ` tone-${tone}` : ''}`}>
      {ts ? (
        <span className="lg-line__ts t-data">{ts}</span>
      ) : (
        <span className="lg-line__ts lg-line__ts--empty t-data" />
      )}
      <span className={`lg-line__lvl lg-line__lvl--${lvl}`}>{level}</span>
      <span className="lg-line__msg">
        {segments.map((seg, i) =>
          seg.match ? (
            <mark className="lg-line__match" key={i}>
              {seg.text}
            </mark>
          ) : (
            <span key={i}>{seg.text}</span>
          ),
        )}
      </span>
    </div>
  )
}

export function LogsPage() {
  const rpc = useRpc()

  useEffect(() => {
    document.title = 'Logs - AgentOS Control'
  }, [])

  // Accumulated tail buffer is React state (rendered data): the poll appends to
  // it via a functional update, mirroring the legacy _allLines module array.
  // The cursor is a ref (poll-loop bookkeeping only, never rendered), matching
  // legacy _cursor.
  const [lines, setLines] = useState<LogLine[]>([])
  const cursorRef = useRef(0)

  // logs.js:16-19 — the first tail hasn't landed yet -> loading placeholder.
  const [tailLoaded, setTailLoaded] = useState(false)

  // Filter state (logs.js:12,17-18).
  const [activeLevels, setActiveLevels] = useState<Set<Level>>(() => new Set(DEFAULT_LEVELS))
  const [search, setSearch] = useState('')
  const [autoFollow, setAutoFollow] = useState(true)

  // logs.js:19-20,208-216 — reentrancy guard + one-shot poll-error toast flag.
  const pollInFlightRef = useRef(false)
  const pollErrorShownRef = useRef(false)
  const displayRef = useRef<HTMLDivElement | null>(null)

  // logs.js:164-172 — logs.status via a read query (rendered as pills). Null on
  // failure so the "unavailable" pill shows. No polling (status is a one-shot
  // read; the tail poll owns the cadence).
  const statusQuery = useQuery<LogsStatus | null>({
    queryKey: ['logs.status'],
    queryFn: async () => {
      await rpc.waitForConnection()
      try {
        return await rpc.call<LogsStatus>('logs.status', {})
      } catch {
        return null
      }
    },
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
  })

  // logs.js:174-217 — one tail poll: fetch the next window, normalize + append,
  // advance the cursor, clamp the ring buffer, and manage the one-shot error
  // toast. Guarded against overlap by pollInFlightRef.
  const poll = useCallback(async () => {
    if (pollInFlightRef.current) return
    pollInFlightRef.current = true
    try {
      const data = await rpc.call<{ lines?: unknown[]; entries?: unknown[]; cursor?: number }>(
        'logs.tail',
        { limit: TAIL_LIMIT, cursor: cursorRef.current, level: null },
      )
      const rawLines = extractLines(data)
      if (rawLines.length > 0) {
        // logs.js:184-188 — advance from data.cursor if present, else by count.
        cursorRef.current =
          data && typeof data.cursor === 'number'
            ? data.cursor
            : cursorRef.current + rawLines.length
        const appended = rawLines.map(normalizeEntry)
        setLines((prev) => clampBuffer(prev.concat(appended)))
      }
      // logs.js:202-207 — the first poll (even empty) clears the loading state.
      setTailLoaded(true)
      pollErrorShownRef.current = false // logs.js:208
    } catch (err) {
      // logs.js:209-213 — one warn toast per error run, suppressed until a
      // successful poll clears the flag.
      if (!pollErrorShownRef.current) {
        const message = err instanceof Error ? err.message : 'unknown error'
        toast.warning('Log refresh failed: ' + message, {
          id: 'logs-refresh-err',
          duration: 2500,
        })
        pollErrorShownRef.current = true
      }
    } finally {
      pollInFlightRef.current = false
    }
  }, [rpc])

  // logs.js:133-137,152-162 — kick the first tail once connected, then poll on
  // the 3000ms cadence. waitForConnection gates the first call (legacy _loadData
  // awaited it before the first _poll).
  useEffect(() => {
    let cancelled = false
    let intervalId: ReturnType<typeof setInterval> | undefined
    void (async () => {
      await rpc.waitForConnection()
      if (cancelled) return
      await poll()
      if (cancelled) return
      intervalId = setInterval(() => void poll(), POLL_MS)
    })()
    return () => {
      cancelled = true
      if (intervalId) clearInterval(intervalId)
    }
  }, [rpc, poll])

  // Derived render data (logs.js:229-260,296-323).
  const filtered = useMemo(
    () => filterLines(lines, activeLevels, search),
    [lines, activeLevels, search],
  )
  const counts = useMemo(() => countByLevel(lines), [lines])

  // logs.js:126-129,322,332-335 — autoscroll to the bottom after the rendered
  // set changes while auto-follow is on. useLayoutEffect so scrollTop is set
  // against the freshly committed DOM height.
  useLayoutEffect(() => {
    if (!autoFollow) return
    const el = displayRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [autoFollow, filtered])

  function toggleLevel(level: Level): void {
    // logs.js:104-119 — add/remove the level from the active set.
    setActiveLevels((prev) => {
      const next = new Set(prev)
      if (next.has(level)) next.delete(level)
      else next.add(level)
      return next
    })
  }

  function onExport(): void {
    // logs.js:337-354 — export the currently filtered lines as a text download.
    const text = buildExportText(filtered)
    const blob = new Blob([text], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'agentos-logs.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  const hasAnyLines = lines.length > 0
  let streamBody
  if (!tailLoaded) {
    // logs.js:94-99 — loading placeholder before the first tail.
    streamBody = (
      <div className="lg-display__placeholder">
        <span className="lg-spinner" aria-hidden="true" />
        Loading logs…
      </div>
    )
  } else if (filtered.length === 0) {
    // logs.js:302-309 — empty vs no-match placeholder.
    streamBody = (
      <div className="lg-display__placeholder">
        <span className="lg-display__placeholder-icon" aria-hidden="true">
          <ScrollTextIcon />
        </span>
        {hasAnyLines ? 'No lines match the current filter.' : 'No logs yet.'}
      </div>
    )
  } else {
    streamBody = filtered.map((line, i) => (
      <LogLineRow line={line} search={search} key={`${line.ts ?? ''}-${i}`} />
    ))
  }

  return (
    <div className="lg-stage">
      <header className="lg-stage__header">
        <AsciiField />
        <div className="lg-stage__title-block">
          <span className="t-label">Control · Logs</span>
          <h2 className="t-display">Logs</h2>
          <p className="lg-stage__subtitle">
            Live gateway log stream — filter, follow, and export.
          </p>
        </div>
        <div className="lg-stage__actions">
          <StatusPills status={statusQuery.data ?? null} />
          <Button
            variant="outline"
            title="Download filtered log lines"
            className="text-xs uppercase tracking-[0.14em]"
            onClick={onExport}
          >
            <DownloadIcon />
            <span>Export</span>
          </Button>
        </div>
      </header>

      <section className="lg-stats" aria-label="Log summary">
        <div className="lg-stat lg-stat--hero" aria-label="In view">
          <span className="lg-stat__label t-label">In view</span>
          <strong className="lg-stat__value t-data">{filtered.length.toLocaleString()}</strong>
          <span className="lg-stat__hint">of {counts.total.toLocaleString()} loaded</span>
        </div>
        <div className={`lg-stat${counts.errors ? ' tone-danger' : ''}`} aria-label="Errors">
          <span className="lg-stat__label t-label">Errors</span>
          <strong className="lg-stat__value t-data">{counts.errors}</strong>
          <span className="lg-stat__hint">{counts.errors ? 'review needed' : 'all clear'}</span>
        </div>
        <div className={`lg-stat${counts.warns ? ' tone-warn' : ''}`} aria-label="Warnings">
          <span className="lg-stat__label t-label">Warnings</span>
          <strong className="lg-stat__value t-data">{counts.warns}</strong>
          <span className="lg-stat__hint">{counts.warns ? 'recent advisories' : 'none'}</span>
        </div>
        <div className="lg-stat" aria-label="Info and Debug">
          <span className="lg-stat__label t-label">Info / Debug</span>
          <strong className="lg-stat__value t-data">
            {counts.infos}
            <span className="lg-stat__sep">/</span>
            {counts.debug}
          </strong>
          <span className="lg-stat__hint">routine output</span>
        </div>
      </section>

      <section className="lg-toolbar panel">
        <div className="lg-levels">
          <span className="lg-toolbar__label t-label">Levels</span>
          <div className="lg-levels__row">
            {LEVELS.map((level) => {
              const isActive = activeLevels.has(level)
              return (
                <button
                  type="button"
                  key={level}
                  className={`lg-level-btn lg-level-btn--${level.toLowerCase()}${isActive ? ' is-active' : ''}`}
                  aria-label={`Toggle ${level} level`}
                  aria-pressed={isActive}
                  onClick={() => toggleLevel(level)}
                >
                  <span className="lg-level-btn__dot" aria-hidden="true" />
                  <span className="lg-level-btn__label">{level}</span>
                </button>
              )
            })}
          </div>
        </div>
        <div className="lg-search-wrap">
          <span className="lg-search-icon" aria-hidden="true">
            <SearchIcon />
          </span>
          <input
            className="lg-search-input"
            type="search"
            aria-label="Filter log messages"
            placeholder="Filter messages…"
            autoComplete="off"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <label className="lg-toggle">
          <input
            type="checkbox"
            checked={autoFollow}
            onChange={(e) => setAutoFollow(e.target.checked)}
          />
          <span className="lg-toggle__track" aria-hidden="true">
            <span className="lg-toggle__thumb" />
          </span>
          <span className="lg-toggle__label">Auto-follow</span>
        </label>
      </section>

      <section className="lg-stream">
        <div
          ref={displayRef}
          className="lg-display"
          role="log"
          aria-live="polite"
          aria-relevant="additions text"
        >
          {streamBody}
        </div>
      </section>
    </div>
  )
}
