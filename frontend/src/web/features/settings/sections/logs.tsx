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
import { Button } from '@/web/components/ui/button'
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

const LEVEL_CLASS: Partial<Record<Level, string>> = {
  ERROR: 'text-danger',
  WARN: 'text-warn',
  TRACE: 'text-text-muted',
  DEBUG: 'text-text-muted',
}

function Pill({ label, warn }: { label: string; warn?: boolean }) {
  return (
    <li
      className={cn(
        'rounded-full border px-2 py-0.5 text-small',
        warn ? 'border-warn/50 text-warn' : 'border-border text-text-muted',
      )}
    >
      {label}
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
    <div className="flex gap-2 px-3 py-0.5 hover:bg-surface-2">
      <span className="w-48 shrink-0 text-text-muted max-sm:hidden">{ts}</span>
      <span className={cn('w-12 shrink-0 font-semibold', LEVEL_CLASS[line.level])}>
        {line.level}
      </span>
      <span className="min-w-0 flex-1 break-words whitespace-pre-wrap">
        {splitHighlight(line.message, search).map((seg, i) =>
          seg.match ? (
            <mark key={i} className="rounded-sm bg-accent/30 text-text">
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
        <Button variant="outline" onClick={() => exportLines(filtered)} disabled={!filtered.length}>
          <Download aria-hidden />
          {t('webSetSystem.logExport')}
        </Button>
      </div>

      <ul
        role="list"
        aria-label={t('webSetSystem.logStats')}
        className="grid grid-cols-2 gap-2 sm:grid-cols-4"
      >
        {stats.map((s) => (
          <li key={s.label} className="rounded-md bg-surface-2 px-3 py-2">
            <span className="block text-small text-text-muted">{s.label}</span>
            <strong className={cn('text-lg', s.tone)}>{s.value}</strong>
          </li>
        ))}
      </ul>

      <div className="flex flex-wrap items-center gap-3">
        <div role="group" aria-label={t('webSetSystem.logLevels')} className="flex flex-wrap gap-1">
          {LEVELS.map((level) => (
            <Button
              key={level}
              size="sm"
              variant={levels.has(level) ? 'secondary' : 'ghost'}
              aria-pressed={levels.has(level)}
              aria-label={t('webSetSystem.logToggleLevel', { level })}
              onClick={() => toggleLevel(level)}
              className={cn('font-mono', !levels.has(level) && 'text-text-muted line-through')}
            >
              {level}
            </Button>
          ))}
        </div>
        <label data-field-id="logSearch" className="relative min-w-48 flex-1" htmlFor={searchId}>
          <span className="sr-only">{t('webSetSystem.logSearch')}</span>
          <Search
            className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-text-muted"
            aria-hidden
          />
          <input
            id={searchId}
            type="search"
            autoComplete="off"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('webSetSystem.logSearchPlaceholder')}
            className="h-9 w-full rounded-md border border-border-control bg-surface-0 pr-2 pl-8 touch:h-11 touch:text-base"
          />
        </label>
        <div data-field-id="logFollow" className="flex items-center gap-2">
          <Switch id={followId} checked={follow} onCheckedChange={setFollow} />
          <label htmlFor={followId}>{t('webSetSystem.logFollow')}</label>
        </div>
      </div>

      {error ? (
        <p role="status" className="text-small text-warn">
          {t('webSetSystem.logPollFailed', { message: error })}
        </p>
      ) : null}

      <div
        ref={display}
        role="log"
        aria-label={t('webSetSystem.logStream')}
        aria-busy={!loaded}
        tabIndex={0}
        className="h-[28rem] overflow-y-auto rounded-lg border border-border bg-surface-0 py-2 font-mono text-small"
      >
        {!loaded ? (
          <p className="px-3 text-text-muted">{t('webSetSystem.logLoading')}</p>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-12 text-text-muted">
            <ScrollText className="size-6" aria-hidden />
            {lines.length ? t('webSetSystem.logNoMatch') : t('webSetSystem.logEmpty')}
          </div>
        ) : (
          filtered.map((line, i) => (
            <LineRow key={`${line.ts ?? ''}-${i}`} line={line} search={search} />
          ))
        )}
      </div>
      <p className="text-small text-text-muted">{t('webSetSystem.logCadence')}</p>
    </div>
  )
}
