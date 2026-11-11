import { useQuery } from '@tanstack/react-query'
import { Loader2, Lock, Radio, RefreshCw, Send, Trash2 } from 'lucide-react'
import { useEffect, useId, useState, type FormEvent } from 'react'
import { useBootstrap, useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSetSystem'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { Badge } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Input, Textarea } from '@/web/components/ui/input'
import { SectionHeader } from '@/web/components/ui/section-header'
import { EmptyState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { Disclosure, Notice } from '../ui'

// System › Debug: raw read-outs of the gateway for troubleshooting. The
// manual RPC tool exists only when the gateway's bootstrap enables
// diagnostics (`features.diagnostics`).

/** Read-only methods shown as snapshots. Names are listed in required-methods. */
const DEBUG_SNAPSHOT_METHODS = [
  'status',
  'health',
  'doctor.status',
  'models.list',
  'last-heartbeat',
] as const

const DEBUG_EVENT_LIMIT = 250

interface GatewayEvent {
  id: number
  at: number
  name: string
  payload: unknown
}

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value)
  } catch {
    return String(value)
  }
}

function errorText(error: unknown): string {
  const code = (error as { code?: unknown } | null)?.code
  const message = error instanceof Error ? error.message : String(error)
  return typeof code === 'string' && code ? `${code}: ${message}` : message
}

function Snapshot({ method }: { method: string }) {
  const rpc = useRpc()
  const connection = useDisplayedConnection()
  const refresh = gate(requireConnection(connection))
  const query = useQuery({
    queryKey: ['debug', method],
    queryFn: () => rpc.call(method, {}),
    retry: false,
    refetchOnWindowFocus: false,
  })
  return (
    <section aria-label={method} className="set-card flex flex-col gap-2.5 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="set-key font-semibold">{method}</h3>
        <span className="flex-1" />
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label={t('webSetSystem.debugRefresh', { method })}
          disabled={refresh.disabled || query.isFetching}
          onClick={() => void query.refetch()}
        >
          <RefreshCw className={query.isFetching ? 'animate-spin' : undefined} />
        </Button>
        {refresh.reason ? (
          <span className="text-small text-text-muted">{refresh.reason}</span>
        ) : null}
      </div>
      {query.isPending ? (
        <LoadingRegion label={t('webSetSystem.loading')} className="flex flex-col gap-1.5">
          <Skeleton className="h-3.5 w-3/5" />
          <Skeleton className="h-3.5 w-2/5" />
          <Skeleton className="h-3.5 w-1/2" />
        </LoadingRegion>
      ) : query.isError ? (
        <Notice role="alert" tone="danger" className="font-mono text-small">
          {errorText(query.error)}
        </Notice>
      ) : (
        <pre tabIndex={0} className="set-log max-h-64 overflow-auto px-3 py-2">
          {pretty(query.data)}
        </pre>
      )}
    </section>
  )
}

/** The newest gateway events first, capped at DEBUG_EVENT_LIMIT. */
function EventLog() {
  const rpc = useRpc()
  const [events, setEvents] = useState<GatewayEvent[]>([])

  useEffect(() => {
    let seq = 0
    return rpc.on('*', (name: unknown, payload: unknown) => {
      const event = { id: ++seq, at: Date.now(), name: String(name), payload }
      setEvents((prev) => [event, ...prev].slice(0, DEBUG_EVENT_LIMIT))
    })
  }, [rpc])

  return (
    <section aria-labelledby="debug-events" className="flex flex-col gap-3">
      <SectionHeader
        id="debug-events"
        title={t('webSetSystem.debugEvents', { limit: DEBUG_EVENT_LIMIT })}
        actions={
          <Button size="sm" variant="ghost" disabled={!events.length} onClick={() => setEvents([])}>
            <Trash2 aria-hidden />
            {t('webSetSystem.debugClear')}
          </Button>
        }
      />
      {events.length ? (
        <ol
          aria-label={t('webSetSystem.debugEventsList')}
          className="set-log max-h-96 overflow-auto py-1"
        >
          {events.map((e) => (
            <li key={e.id} className="px-3 [&+&]:border-t [&+&]:border-border">
              <Disclosure
                summary={
                  <span className="min-w-0 truncate font-normal">
                    <span className="text-text-muted tabular-nums">
                      {new Date(e.at).toLocaleTimeString()}
                    </span>{' '}
                    <span className="text-text">{e.name}</span>
                  </span>
                }
                className="[&>summary]:min-h-8 [&>summary]:py-1"
                bodyClassName="pt-0 pb-2"
              >
                <pre tabIndex={0} className="overflow-auto py-1 pl-6">
                  {pretty(e.payload)}
                </pre>
              </Disclosure>
            </li>
          ))}
        </ol>
      ) : (
        <EmptyState
          size="sm"
          icon={<Radio />}
          title={t('webSetSystem.debugNoEvents')}
          className="set-card"
        />
      )}
    </section>
  )
}

/** One method + JSON params, answered with the formatted payload or error. */
function ManualRpc() {
  const rpc = useRpc()
  const connection = useDisplayedConnection()
  const methodId = useId()
  const paramsId = useId()
  const [method, setMethod] = useState('')
  const [params, setParams] = useState('{}')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null)
  const send = gate(requireConnection(connection))

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    const name = method.trim()
    if (!name || busy || send.disabled) return
    let parsed: Record<string, unknown>
    try {
      const value: unknown = params.trim() ? JSON.parse(params) : {}
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error()
      parsed = value as Record<string, unknown>
    } catch {
      setResult({ ok: false, text: t('webSetSystem.debugBadParams') })
      return
    }
    setBusy(true)
    try {
      setResult({ ok: true, text: pretty(await rpc.call(name, parsed)) })
    } catch (err) {
      setResult({ ok: false, text: errorText(err) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      onSubmit={(e) => void onSubmit(e)}
      aria-labelledby="debug-rpc"
      className="set-card flex flex-col gap-3"
    >
      <div>
        <h2 id="debug-rpc" className="set-card-title">
          {t('webSetSystem.debugRpc')}
        </h2>
        <p className="mt-0.5 text-small text-text-muted">{t('webSetSystem.debugRpcHelp')}</p>
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor={methodId} className="font-medium">
          {t('webSetSystem.debugMethod')}
        </label>
        <Input
          id={methodId}
          value={method}
          onChange={(e) => setMethod(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          className="font-mono"
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor={paramsId} className="font-medium">
          {t('webSetSystem.debugParams')}
        </label>
        <Textarea
          id={paramsId}
          rows={4}
          value={params}
          onChange={(e) => setParams(e.target.value)}
          spellCheck={false}
          className="font-mono text-small"
        />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" variant="primary" disabled={!method.trim() || busy || send.disabled}>
          {busy ? <Loader2 className="animate-spin" aria-hidden /> : <Send aria-hidden />}
          {t('webSetSystem.debugSend')}
        </Button>
        {send.reason ? <span className="text-small text-text-muted">{send.reason}</span> : null}
      </div>
      {result ? (
        <div className="flex flex-col gap-1">
          <Badge tone={result.ok ? 'ok' : 'danger'} dot className="self-start">
            {result.ok ? t('webSetSystem.debugResponse') : t('webSetSystem.debugError')}
          </Badge>
          <pre
            tabIndex={0}
            data-testid="debug-rpc-result"
            role={result.ok ? undefined : 'alert'}
            className="set-log anim-fade-in max-h-96 overflow-auto px-3 py-2"
          >
            {result.text}
          </pre>
        </div>
      ) : null}
    </form>
  )
}

export function DebugSection() {
  const bootstrap = useBootstrap()
  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="debug-snapshots" className="flex flex-col gap-3">
        <SectionHeader id="debug-snapshots" title={t('webSetSystem.debugSnapshots')} />
        {DEBUG_SNAPSHOT_METHODS.map((method) => (
          <Snapshot key={method} method={method} />
        ))}
      </section>
      <EventLog />
      {bootstrap.features?.diagnostics ? (
        <ManualRpc />
      ) : (
        <Notice tone="info" icon={<Lock />} className="text-small">
          {t('webSetSystem.debugRpcOff')}
        </Notice>
      )}
    </div>
  )
}
