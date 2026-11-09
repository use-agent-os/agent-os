import { useQuery } from '@tanstack/react-query'
import { Loader2, RefreshCw, Send, Trash2 } from 'lucide-react'
import { useEffect, useId, useState, type FormEvent } from 'react'
import { useBootstrap, useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSetSystem'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { Button } from '@/web/components/ui/button'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'

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
    <section aria-label={method} className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-mono font-semibold">{method}</h3>
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
        <p className="text-small text-text-muted">{t('webSetSystem.loading')}</p>
      ) : query.isError ? (
        <p role="alert" className="font-mono text-small text-danger">
          {errorText(query.error)}
        </p>
      ) : (
        <pre className="max-h-64 overflow-auto rounded-md border border-border bg-surface-0 p-2 font-mono text-small">
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
    <section aria-labelledby="debug-events" className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h2 id="debug-events" className="text-base font-semibold">
          {t('webSetSystem.debugEvents', { limit: DEBUG_EVENT_LIMIT })}
        </h2>
        <Button size="sm" variant="ghost" disabled={!events.length} onClick={() => setEvents([])}>
          <Trash2 aria-hidden />
          {t('webSetSystem.debugClear')}
        </Button>
      </div>
      {events.length ? (
        <ol
          aria-label={t('webSetSystem.debugEventsList')}
          className="max-h-96 overflow-auto rounded-md border border-border bg-surface-0 font-mono text-small"
        >
          {events.map((e) => (
            <li key={e.id} className="border-b border-border px-2 py-1 last:border-b-0">
              <details>
                <summary className="cursor-pointer">
                  <span className="text-text-muted">{new Date(e.at).toLocaleTimeString()}</span>{' '}
                  {e.name}
                </summary>
                <pre className="overflow-auto py-1">{pretty(e.payload)}</pre>
              </details>
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-small text-text-muted">{t('webSetSystem.debugNoEvents')}</p>
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
      className="flex flex-col gap-3"
    >
      <h2 id="debug-rpc" className="text-base font-semibold">
        {t('webSetSystem.debugRpc')}
      </h2>
      <p className="text-small text-text-muted">{t('webSetSystem.debugRpcHelp')}</p>
      <div className="flex flex-col gap-1">
        <label htmlFor={methodId} className="font-medium">
          {t('webSetSystem.debugMethod')}
        </label>
        <input
          id={methodId}
          value={method}
          onChange={(e) => setMethod(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          className="h-9 w-full max-w-xl rounded-md border border-border-control bg-surface-0 px-3 font-mono touch:h-11"
        />
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={paramsId} className="font-medium">
          {t('webSetSystem.debugParams')}
        </label>
        <textarea
          id={paramsId}
          rows={4}
          value={params}
          onChange={(e) => setParams(e.target.value)}
          spellCheck={false}
          className="w-full max-w-xl rounded-md border border-border-control bg-surface-0 px-3 py-2 font-mono text-small"
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
          <p className={result.ok ? 'text-small text-ok' : 'text-small text-danger'}>
            {result.ok ? t('webSetSystem.debugResponse') : t('webSetSystem.debugError')}
          </p>
          <pre
            data-testid="debug-rpc-result"
            role={result.ok ? undefined : 'alert'}
            className="max-h-96 overflow-auto rounded-md border border-border bg-surface-0 p-2 font-mono text-small"
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
      <section aria-labelledby="debug-snapshots" className="flex flex-col gap-4">
        <h2 id="debug-snapshots" className="text-base font-semibold">
          {t('webSetSystem.debugSnapshots')}
        </h2>
        {DEBUG_SNAPSHOT_METHODS.map((method) => (
          <Snapshot key={method} method={method} />
        ))}
      </section>
      <EventLog />
      {bootstrap.features?.diagnostics ? (
        <ManualRpc />
      ) : (
        <p className="text-small text-text-muted">{t('webSetSystem.debugRpcOff')}</p>
      )}
    </div>
  )
}
