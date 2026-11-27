import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  ExternalLink,
  EyeOff,
  Globe,
  Lock,
  MonitorOff,
  PlugZap,
  RotateCw,
  ShieldAlert,
} from 'lucide-react'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webPanel'
import { cn } from '@/lib/utils'
import { Button } from '@/web/components/ui/button'
import { SegmentedControl } from '@/web/components/ui/segmented-control'
import { EmptyState, ErrorState, Skeleton } from '@/web/components/ui/states'
import { SimpleTooltip } from '@/web/components/ui/tooltip'
import '@/web/styles/workspace-panel.css'
import { useGatewayMethods, usePageVisible } from '../hooks'
import type { ToolTabProps } from '../registry'
import {
  browserFailure,
  HISTORY_METHOD,
  NAVIGATE_METHOD,
  normalizeAddress,
  normalizeSnapshot,
  normalizeStatus,
  SELECT_TAB_METHOD,
  SNAPSHOT_METHOD,
  START_METHOD,
  STATUS_METHOD,
  type BrowserSnapshot,
  type BrowserStatus,
} from './logic'

/** How often the page image refreshes while the tab is on screen. */
export const SNAPSHOT_INTERVAL_MS = 2_000
/** How often a stopped browser is checked for an agent having started it. */
export const STATUS_INTERVAL_MS = 8_000

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function ToolbarButton(props: {
  label: string
  onClick(): void
  disabled?: boolean
  spinning?: boolean
  children: React.ReactNode
}) {
  return (
    <SimpleTooltip side="bottom" label={props.label}>
      <button
        type="button"
        aria-label={props.label}
        disabled={props.disabled}
        onClick={props.onClick}
        className={cn('ws-icon-button target', props.spinning && 'ws-spinning')}
      >
        {props.children}
      </button>
    </SimpleTooltip>
  )
}

function State(props: {
  icon: React.ReactNode
  title: string
  body?: React.ReactNode
  action?: React.ReactNode
}) {
  return (
    <div className="ws-state">
      <EmptyState
        size="sm"
        icon={props.icon}
        title={props.title}
        description={props.body}
        action={props.action}
      />
    </div>
  )
}

/**
 * The Browser tab: a live view of the browser the session's agent drives
 * through its browser tool. A page image refreshed every two seconds while
 * the tab is on screen, the browser's tabs, an address bar and
 * back/forward/reload. Clicks and typing on the page stay with the agent.
 */
export function BrowserTab({ sessionKey, active }: ToolTabProps) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const methods = useGatewayMethods()
  const pageVisible = usePageVisible()
  const watching = active && pageVisible
  const advertised = !methods || methods.includes(STATUS_METHOD)
  const statusKey = ['browser-view', sessionKey, 'status'] as const
  const snapshotKey = ['browser-view', sessionKey, 'snapshot'] as const

  const status = useQuery<BrowserStatus>({
    queryKey: statusKey,
    queryFn: async () => {
      await rpc.waitForConnection()
      return normalizeStatus(await rpc.call<unknown>(STATUS_METHOD, { sessionKey }))
    },
    enabled: advertised,
    retry: false,
    refetchInterval: (query) =>
      watching && query.state.data && !query.state.data.running ? STATUS_INTERVAL_MS : false,
  })
  const running = status.data?.running === true

  const snapshot = useQuery<BrowserSnapshot>({
    queryKey: snapshotKey,
    queryFn: async () => {
      await rpc.waitForConnection()
      return normalizeSnapshot(await rpc.call<unknown>(SNAPSHOT_METHOD, { sessionKey }))
    },
    enabled: advertised && running && watching,
    retry: false,
    placeholderData: keepPreviousData,
    refetchInterval: watching ? SNAPSHOT_INTERVAL_MS : false,
    refetchIntervalInBackground: false,
  })

  // A browser that went away (closed, reaped) reads as stopped again.
  useEffect(() => {
    if (snapshot.data && !snapshot.data.running) {
      void queryClient.invalidateQueries({ queryKey: statusKey })
    }
    // statusKey is rebuilt each render; the session key is what matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot.data, queryClient, sessionKey])

  const refreshNow = () => void queryClient.invalidateQueries({ queryKey: snapshotKey })
  const [actionError, setActionError] = useState<string | null>(null)
  const act = useMutation({
    mutationFn: async ({ method, params }: { method: string; params: Record<string, unknown> }) =>
      rpc.call<unknown>(method, { sessionKey, ...params }),
    onMutate: () => setActionError(null),
    onSuccess: () => refreshNow(),
    onError: (error) => setActionError(errorMessage(error)),
  })
  const start = useMutation({
    mutationFn: async () => normalizeStatus(await rpc.call<unknown>(START_METHOD, { sessionKey })),
    onSuccess: (next) => {
      queryClient.setQueryData(statusKey, next)
      refreshNow()
    },
  })

  const current = snapshot.data?.running ? snapshot.data : undefined
  const [zoom, setZoom] = useState<'fit' | 'actual'>('fit')
  const [draft, setDraft] = useState<string | null>(null)
  const [addressError, setAddressError] = useState<string | null>(null)
  const address = draft ?? current?.url ?? ''
  const input = useRef<HTMLInputElement>(null)

  if (!advertised) {
    return (
      <State
        icon={<MonitorOff />}
        title={t('webPanel.browserOutdatedTitle')}
        body={t('webPanel.browserOutdatedBody')}
      />
    )
  }
  if (status.isPending) {
    return (
      <div className="ws-state" role="status" aria-label={t('webPanel.browserLoading')}>
        <Skeleton className="h-40 w-full max-w-md rounded-2xl" />
      </div>
    )
  }
  if (status.isError) {
    const failure = browserFailure(status.error)
    if (failure === 'outdated') {
      return (
        <State
          icon={<MonitorOff />}
          title={t('webPanel.browserOutdatedTitle')}
          body={t('webPanel.browserOutdatedBody')}
        />
      )
    }
    return (
      <div className="p-3">
        <ErrorState
          message={errorMessage(status.error)}
          onRetry={() => void status.refetch()}
          retrying={status.isFetching}
        />
      </div>
    )
  }
  const info = status.data
  if (!info.enabled) {
    return (
      <State
        icon={<PlugZap />}
        title={t('webPanel.browserDisabledTitle')}
        body={t('webPanel.browserDisabledBody')}
      />
    )
  }
  if (!info.installed) {
    return (
      <State
        icon={<AlertTriangle />}
        title={t('webPanel.browserNotInstalledTitle')}
        body={
          <>
            {t('webPanel.browserNotInstalledBody')}
            {info.installHint ? (
              <code className="ws-code-hint">{info.installHint.replace(/^.*?:\s*/, '')}</code>
            ) : null}
          </>
        }
      />
    )
  }
  if (!info.attachConfirmed) {
    return (
      <State
        icon={<ShieldAlert />}
        title={t('webPanel.browserAttachTitle')}
        body={t('webPanel.browserAttachBody')}
      />
    )
  }
  if (!running) {
    return (
      <State
        icon={<Globe />}
        title={t('webPanel.browserNotRunningTitle')}
        body={
          <>
            {t('webPanel.browserNotRunningBody')}
            {start.isError ? (
              <span role="alert" className="mt-2 block text-danger">
                {t('webPanel.browserStartFailed', { message: errorMessage(start.error) })}
              </span>
            ) : null}
          </>
        }
        action={
          info.canStart ? (
            <Button
              variant="primary"
              size="sm"
              onClick={() => start.mutate()}
              disabled={start.isPending}
              aria-busy={start.isPending || undefined}
            >
              {start.isPending ? t('webPanel.browserStarting') : t('webPanel.browserStart')}
            </Button>
          ) : null
        }
      />
    )
  }

  const busy = act.isPending || snapshot.isFetching
  const submit = (e: FormEvent) => {
    e.preventDefault()
    const url = normalizeAddress(address)
    if (!url) {
      setAddressError(t('webPanel.browserInvalidAddress'))
      return
    }
    setAddressError(null)
    setDraft(null)
    input.current?.blur()
    act.mutate({ method: NAVIGATE_METHOD, params: { url } })
  }
  const external = current?.url && /^https?:\/\//i.test(current.url) ? current.url : null
  const tabs = current?.tabs ?? []
  const stageId = `ws-browser-stage-${sessionKey.replace(/[^A-Za-z0-9_-]/g, '_')}`
  const refreshError = snapshot.isError ? errorMessage(snapshot.error) : null

  let stage: React.ReactNode
  const blank = current !== undefined && (!current.url || current.url === 'about:blank')
  if (blank) {
    // A blank tab's frame is a white rectangle: say what it is instead.
    stage = (
      <State
        icon={<Globe />}
        title={t('webPanel.browserBlankTitle')}
        body={t('webPanel.browserBlankBody')}
      />
    )
  } else if (current?.image) {
    stage = (
      <img
        src={`data:${current.image.mime};base64,${current.image.data}`}
        alt={
          current.title
            ? t('webPanel.browserPage', { title: current.title })
            : t('webPanel.browserPageUntitled')
        }
        className="ws-browser-image"
        // Inline: the base stylesheet caps every image at its box.
        style={
          zoom === 'actual'
            ? { width: 'auto', maxWidth: 'none', alignSelf: 'flex-start', flexShrink: 0 }
            : undefined
        }
        draggable={false}
      />
    )
  } else if (current?.withheld) {
    stage = (
      <State
        icon={<EyeOff />}
        title={t('webPanel.browserWithheldTitle')}
        body={t('webPanel.browserWithheldBody')}
      />
    )
  } else if (snapshot.isError && !current) {
    stage = (
      <div className="p-3">
        <ErrorState message={refreshError} onRetry={refreshNow} retrying={snapshot.isFetching} />
      </div>
    )
  } else {
    stage = (
      <div className="ws-browser-skeleton" role="status" aria-label={t('webPanel.browserLoading')}>
        <Skeleton className="h-full w-full rounded-xl" />
      </div>
    )
  }

  return (
    <div className="ws-browser">
      <form className="ws-toolbar ws-browser-bar" onSubmit={submit}>
        <ToolbarButton
          label={t('webPanel.browserBack')}
          onClick={() => act.mutate({ method: HISTORY_METHOD, params: { action: 'back' } })}
          disabled={act.isPending}
        >
          <ArrowLeft />
        </ToolbarButton>
        <ToolbarButton
          label={t('webPanel.browserForward')}
          onClick={() => act.mutate({ method: HISTORY_METHOD, params: { action: 'forward' } })}
          disabled={act.isPending}
        >
          <ArrowRight />
        </ToolbarButton>
        <ToolbarButton
          label={t('webPanel.browserReload')}
          onClick={() => act.mutate({ method: HISTORY_METHOD, params: { action: 'reload' } })}
          disabled={act.isPending}
          spinning={busy}
        >
          <RotateCw />
        </ToolbarButton>
        <div className="ws-field-wrap">
          {external?.startsWith('https:') ? <Lock aria-hidden /> : <Globe aria-hidden />}
          <input
            ref={input}
            type="text"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            value={address}
            onChange={(e) => {
              setDraft(e.target.value)
              setAddressError(null)
            }}
            onFocus={(e) => e.currentTarget.select()}
            onBlur={() => {
              if (draft !== null && draft === (current?.url ?? '')) setDraft(null)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Escape' && (draft !== null || addressError)) {
                e.preventDefault()
                setDraft(null)
                setAddressError(null)
              }
            }}
            aria-label={t('webPanel.browserAddress')}
            aria-invalid={addressError ? true : undefined}
            aria-describedby={addressError ? `${stageId}-address-error` : undefined}
            placeholder={t('webPanel.browserAddressPlaceholder')}
            className="ws-field ws-address"
          />
        </div>
        {external ? (
          <SimpleTooltip side="bottom" label={t('webPanel.browserOpenExternal')}>
            <a
              href={external}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={t('webPanel.browserOpenExternal')}
              className="ws-icon-button target"
            >
              <ExternalLink />
            </a>
          </SimpleTooltip>
        ) : null}
      </form>
      {addressError ? (
        <p id={`${stageId}-address-error`} role="alert" className="ws-note" data-tone="danger">
          <AlertTriangle aria-hidden />
          {addressError}
        </p>
      ) : null}
      {tabs.length > 1 ? (
        <div role="tablist" aria-label={t('webPanel.browserTabs')} className="ws-btabs">
          {tabs.map((tab) => (
            <button
              key={tab.tabId}
              type="button"
              role="tab"
              aria-selected={tab.tabId === current?.activeTabId}
              aria-controls={stageId}
              tabIndex={tab.tabId === current?.activeTabId ? 0 : -1}
              title={tab.url}
              onClick={() =>
                act.mutate({ method: SELECT_TAB_METHOD, params: { tabId: tab.tabId } })
              }
              onKeyDown={(e) => {
                const index = tabs.indexOf(tab)
                const next =
                  e.key === 'ArrowRight' ? index + 1 : e.key === 'ArrowLeft' ? index - 1 : null
                if (next === null) return
                e.preventDefault()
                const to = tabs[(next + tabs.length) % tabs.length]
                const sibling = e.currentTarget.parentElement?.querySelector<HTMLElement>(
                  `[data-tab-id="${to?.tabId}"]`,
                )
                sibling?.focus()
              }}
              data-tab-id={tab.tabId}
              className="ws-btab target"
            >
              <Globe aria-hidden />
              <span className="truncate">{tab.title || tab.url || tab.tabId}</span>
            </button>
          ))}
        </div>
      ) : null}
      {current?.truncated ? (
        <p className="ws-note" data-tone="info">
          {t('webPanel.browserMoreTabs')}
        </p>
      ) : null}
      {actionError ? (
        <p role="alert" className="ws-note" data-tone="danger">
          <AlertTriangle aria-hidden />
          {t('webPanel.browserActionFailed', { message: actionError })}
        </p>
      ) : refreshError && current ? (
        <p role="alert" className="ws-note" data-tone="danger">
          <AlertTriangle aria-hidden />
          {t('webPanel.browserRefreshFailed', { message: refreshError })}
        </p>
      ) : null}
      <div
        id={stageId}
        role="region"
        aria-label={t('webPanel.toolBrowser')}
        tabIndex={current?.image && !blank ? 0 : undefined}
        className="ws-browser-stage"
        data-hug={(current?.image && !blank && zoom === 'fit') || undefined}
      >
        {stage}
      </div>
      <div className="ws-browser-foot">
        {/* One paragraph that wraps on a narrow panel: never cut short. */}
        <p className="ws-browser-status">
          <span className="ws-live-dot" data-live={watching || undefined} aria-hidden />
          {watching ? t('webPanel.browserLive') : t('webPanel.browserPaused')}
          <span aria-hidden> · </span>
          {t('webPanel.browserReadOnly')}
        </p>
        {current?.image && !blank ? (
          <SegmentedControl
            size="sm"
            kind="toggle"
            aria-label={t('webPanel.browserZoom')}
            value={zoom}
            onChange={setZoom}
            options={[
              { value: 'fit', label: t('webPanel.browserZoomFit') },
              { value: 'actual', label: t('webPanel.browserZoomActual') },
            ]}
          />
        ) : null}
      </div>
    </div>
  )
}
