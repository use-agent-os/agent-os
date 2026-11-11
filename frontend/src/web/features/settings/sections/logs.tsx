import { useQuery } from '@tanstack/react-query'
import { Download, ScrollText, Search } from 'lucide-react'
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useRpc } from '@/app/providers'
import { formatNumber, t } from '@/i18n'
import '@/i18n/en/webSetSystem'
import { cn } from '@/lib/utils'
import {
  buildExportText,
  clampBuffer,
  countByLevel,
  DEFAULT_LEVELS,
  extractLines,
  filterLines,
  LEVELS,
  normalizeEntry,
  sliceTs,
  splitHighlight,
  type Level,
  type LogLine,
} from '@/views/logs/logic'
import { Badge, StatusDot } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Input } from '@/web/components/ui/input'
import { Skeleton } from '@/web/components/ui/states'
import { Switch } from '@/web/components/ui/switch'
import { useRegisterFields } from '../fields'

// System › Logs: the gateway log tail. `logs.tail` is read by cursor every
// 3 s while the page is visible; the buffer keeps the newest 2000 lines.

const LOGS_POLL_MS = 3000
const TAIL_LIMIT = 500

interface LogsStatus {
  gateway_file_log?: { enabled?: boolean; path?: string }
  raw_turn_call_log?: { enabled?: boolean }
  diagnostics_enabled?: { effective?: boolean; detail?: string }
}

function Pill({ label, warn }: { label: string; warn?: boolean }) {
  return (
    <li>
      <Badge tone={warn ? 'warn' : 'neutral'} dot>
        {label}
      </Badge>
    </li>
  )
}

function StatusPills({ status }: { status: LogsStatus | null | undefined }) {
  if (!status) {
    return (
      <ul role="list" aria-label={t('webSetSystem.logStatus')} className="flex flex-wrap gap-1.5">
        <Pill label={t('webSetSystem.logStatusUnavailable')} warn />
      </ul>
    )
  }
  const onOff = (on?: boolean) => (on ? t('webSetSystem.on') : t('webSetSystem.off'))
  const file = status.gateway_file_log ?? {}
  const raw = status.raw_turn_call_log ?? {}
  const diag = status.diagnostics_enabled ?? {}
  const diagLabel =
    diag.detail === 'raw'
      ? t('webSetSystem.logDiagnosticsRaw')
      : diag.effective
        ? t('webSetSystem.logDiagnosticsStandard')
        : t('webSetSystem.logDiagnosticsOff')
  return (
    <ul role="list" aria-label={t('webSetSystem.logStatus')} className="flex flex-wrap gap-1.5">
      <Pill
        label={t('webSetSystem.logFile', { state: onOff(file.enabled) })}
        warn={!file.enabled}
      />
      <Pill
        label={t('webSetSystem.logRawTurn', { state: onOff(raw.enabled) })}
        warn={!raw.enabled}
      />
      <Pill label={diagLabel} warn={!diag.effective} />
    </ul>
  )
}

function LineRow({ line, search }: { line: LogLine; search: string }) {
  const ts = sliceTs(line.ts)
  return (
    <div className="set-log-line max-sm:grid-cols-[auto_minmax(0,1fr)]">
      <span className="text-text-muted tabular-nums max-sm:hidden">{ts}</span>
      <span className="set-log-level" data-level={line.level}>
        {line.level}
      </span>
      <span className="min-w-0 break-words whitespace-pre-wrap">
        {splitHighlight(line.message, search).map((seg, i) =>
          seg.match ? (
            <mark key={i} className="rounded-sm bg-accent-tint text-text ring-1 ring-accent/40">
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

/** Poll `logs.tail` by cursor, skipping ticks while the document is hidden. */
function useLogTail() {
  const rpc = useRpc()
  const [lines, setLines] = useState<LogLine[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const cursor = useRef(0)
  const inFlight = useRef(false)

  const poll = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const data = await rpc.call<{ cursor?: number }>('logs.tail', {
        limit: TAIL_LIMIT,
        cursor: cursor.current,
        level: null,
      })
      const raw = extractLines(data)
      if (raw.length) {
        cursor.current =
          typeof data?.cursor === 'number' ? data.cursor : cursor.current + raw.length
        setLines((prev) => clampBuffer(prev.concat(raw.map(normalizeEntry))))
      }
      setLoaded(true)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      inFlight.current = false
    }
  }, [rpc])

  useEffect(() => {
    void poll()
    const id = window.setInterval(() => {
      if (!document.hidden) void poll()
    }, LOGS_POLL_MS)
    // Catch up at once when the tab comes back.
    const onVisible = () => {
      if (!document.hidden) void poll()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [poll])

  return { lines, loaded, error }
}

function exportLines(lines: LogLine[]) {
  const blob = new Blob([buildExportText(lines)], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'agentos-logs.txt'
  a.click()
  URL.revokeObjectURL(url)
}

export function LogsSection() {
  const rpc = useRpc()
  const searchId = useId()
  const followId = useId()
  const { lines, loaded, error } = useLogTail()
  const [levels, setLevels] = useState<Set<Level>>(() => new Set(DEFAULT_LEVELS))
  const [search, setSearch] = useState('')
  const [follow, setFollow] = useState(true)
  const display = useRef<HTMLDivElement>(null)

  useRegisterFields('logs', [
    { id: 'logSearch', label: t('webSetSystem.logSearch') },
    { id: 'logFollow', label: t('webSetSystem.logFollow') },
  ])

  const status = useQuery({
    queryKey: ['logs.status'],
    queryFn: () => rpc.call<LogsStatus>('logs.status', {}).catch(() => null),
    retry: false,
  })

  const filtered = useMemo(() => filterLines(lines, levels, search), [lines, levels, search])
  const counts = useMemo(() => countByLevel(lines), [lines])

  useLayoutEffect(() => {
    if (follow && display.current) display.current.scrollTop = display.current.scrollHeight
  }, [follow, filtered])

  const toggleLevel = (level: Level) =>
    setLevels((prev) => {
      const next = new Set(prev)
      if (next.has(level)) next.delete(level)
      else next.add(level)
      return next
    })

  const stats: { label: string; value: string; tone?: string }[] = [
    {
      label: t('webSetSystem.logInView'),
      value: t('webSetSystem.logInViewValue', {
        shown: formatNumber(filtered.length),
        total: formatNumber(counts.total),
      }),
    },
    {
      label: t('webSetSystem.logErrors'),
      value: formatNumber(counts.errors),
      tone: counts.errors ? 'text-danger' : undefined,
    },
    {
      label: t('webSetSystem.logWarnings'),
      value: formatNumber(counts.warns),
      tone: counts.warns ? 'text-warn' : undefined,
    },
    {
      label: t('webSetSystem.logInfoDebug'),
      value: `${formatNumber(counts.infos)} / ${formatNumber(counts.debug)}`,
    },
  ]

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <StatusPills status={status.data} />
        <Button
          variant="outline"
          size="sm"
          onClick={() => exportLines(filtered)}
          disabled={!filtered.length}
        >
          <Download aria-hidden />
          {t('webSetSystem.logExport')}
        </Button>
      </div>

      <ul
        role="list"
        aria-label={t('webSetSystem.logStats')}
        className="grid grid-cols-2 gap-2.5 sm:grid-cols-4"
      >
        {stats.map((s) => (
          <li key={s.label} className="set-card px-3.5 py-2.5">
            <span className="block text-label text-text-muted">{s.label}</span>
            <strong className={cn('text-lg font-semibold tabular-nums', s.tone)}>{s.value}</strong>
          </li>
        ))}
      </ul>

      <section className="set-log overflow-hidden">
        <div className="sticky top-0 z-[1] flex flex-wrap items-center gap-2 border-b border-border bg-surface-1 px-2.5 py-2 font-sans text-ui">
          <div
            role="group"
            aria-label={t('webSetSystem.logLevels')}
            className="flex flex-wrap gap-0.5 rounded-control bg-surface-inset p-0.5"
          >
            {LEVELS.map((level) => (
              <Button
                key={level}
                size="sm"
                variant="ghost"
                aria-pressed={levels.has(level)}
                aria-label={t('webSetSystem.logToggleLevel', { level })}
                onClick={() => toggleLevel(level)}
                className={cn(
                  'h-6 px-2 font-mono text-small',
                  levels.has(level)
                    ? 'bg-surface-3 shadow-highlight'
                    : 'text-text-muted line-through decoration-text-muted/60',
                )}
              >
                <span className="set-log-level min-w-0" data-level={level}>
                  {level}
                </span>
              </Button>
            ))}
          </div>
          <label data-field-id="logSearch" className="min-w-40 flex-1" htmlFor={searchId}>
            <span className="sr-only">{t('webSetSystem.logSearch')}</span>
            <Input
              id={searchId}
              type="search"
              size="sm"
              autoComplete="off"
              icon={<Search />}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('webSetSystem.logSearchPlaceholder')}
            />
          </label>
          <div data-field-id="logFollow" className="flex items-center gap-2 px-1">
            <Switch id={followId} checked={follow} onCheckedChange={setFollow} />
            <label htmlFor={followId} className="text-small">
              {t('webSetSystem.logFollow')}
            </label>
          </div>
        </div>

        {error ? (
          <p
            role="status"
            className="border-b border-border bg-warn/10 px-3 py-1.5 font-sans text-small text-warn"
          >
            {t('webSetSystem.logPollFailed', { message: error })}
          </p>
        ) : null}

        <div
          ref={display}
          role="log"
          aria-label={t('webSetSystem.logStream')}
          aria-busy={!loaded}
          tabIndex={0}
          className="h-[30rem] overflow-y-auto overscroll-contain py-2 focus-visible:-outline-offset-2"
        >
          {!loaded ? (
            <div className="flex flex-col gap-2 px-3 py-1">
              <p className="sr-only">{t('webSetSystem.logLoading')}</p>
              {[72, 54, 88, 61, 45, 80].map((w, i) => (
                <Skeleton key={i} className="h-3.5" style={{ width: `${w}%` }} />
              ))}
            </div>
          ) : filtered.length === 0 ? (
            <div className="flex flex-col items-center gap-3 py-16 font-sans text-text-muted">
              <span className="grid size-10 place-items-center rounded-card bg-surface-2 shadow-highlight">
                <ScrollText className="size-5" aria-hidden />
              </span>
              {lines.length ? t('webSetSystem.logNoMatch') : t('webSetSystem.logEmpty')}
            </div>
          ) : (
            filtered.map((line, i) => (
              <LineRow key={`${line.ts ?? ''}-${i}`} line={line} search={search} />
            ))
          )}
        </div>
        <footer className="flex items-center gap-2 border-t border-border bg-surface-1 px-3 py-1.5 font-sans text-small text-text-muted">
          <StatusDot tone={error ? 'warn' : 'ok'} live={!error && follow} className="size-1.5" />
          {t('webSetSystem.logCadence')}
        </footer>
      </section>
    </div>
  )
}
