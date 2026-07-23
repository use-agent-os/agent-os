import './settings.css'
import { useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import {
  AlertCircleIcon,
  BotIcon,
  CheckCircle2Icon,
  FileCode2Icon,
  LoaderCircleIcon,
  RefreshCwIcon,
  SlidersHorizontalIcon,
  XCircleIcon,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useRpc } from '@/app/providers'
import { SetupPage } from '@/views/setup/SetupPage'
import { ConfigPage } from '@/views/config/ConfigPage'
import {
  loadSettingsSnapshot,
  readinessFromSnapshot,
  SETTINGS_SNAPSHOT_QUERY_KEY,
  type SettingsSnapshot,
} from './snapshot'

type SettingsSurface = 'guided' | 'advanced'

function initialSurface(pathname: string): SettingsSurface {
  return pathname.replace(/\/+$/, '').endsWith('/config') ? 'advanced' : 'guided'
}

export function SettingsPage() {
  const rpc = useRpc()
  const navigate = useNavigate()
  const { pathname, search, hash } = useLocation()
  const [surface, setSurface] = useState<SettingsSurface>(() => initialSurface(pathname))
  const [surfacePath, setSurfacePath] = useState(pathname)

  if (pathname !== surfacePath) {
    setSurfacePath(pathname)
    setSurface(initialSurface(pathname))
  }

  useEffect(() => {
    document.title = 'Agent Settings - AgentOS Control'
  }, [])

  useEffect(() => {
    const requestedStep = new URLSearchParams(search).get('step')
    if (requestedStep === 'channels' || hash === '#channels') {
      navigate('/channels?view=setup', { replace: true })
    }
  }, [hash, navigate, search])

  const snapshotQuery = useQuery<SettingsSnapshot>({
    queryKey: SETTINGS_SNAPSHOT_QUERY_KEY,
    queryFn: () => loadSettingsSnapshot(rpc),
    refetchOnWindowFocus: false,
  })

  const snapshot = snapshotQuery.data
  const snapshotUnavailable = snapshotQuery.isError && !snapshot
  const readiness = useMemo(() => readinessFromSnapshot(snapshot), [snapshot])
  const readinessLabel =
    readiness.total > 0
      ? `${readiness.ready} of ${readiness.total} ready`
      : snapshotQuery.isError
        ? 'Status unavailable'
        : 'Checking setup'
  const activeModel = snapshot?.config?.llm?.model || 'Choose a model'
  const activeProviderId = snapshot?.config?.llm?.provider
  const activeProvider =
    snapshot?.catalog?.providers?.find((provider) => provider.providerId === activeProviderId)
      ?.label ||
    activeProviderId ||
    'Provider not connected'
  const statusLabel = snapshot?.writeBlocked
    ? 'Changes paused'
    : snapshot?.pendingRestart
      ? 'Restart needed'
      : snapshotQuery.isError
        ? 'Status unavailable'
        : readiness.actionRequired > 0
          ? `${readiness.actionRequired} setup item${readiness.actionRequired === 1 ? '' : 's'} left`
          : snapshotQuery.isSuccess
            ? 'Ready to use'
            : 'Checking setup'
  const statusTone = snapshot?.writeBlocked
    ? 'tone-danger'
    : snapshot?.pendingRestart
      ? 'tone-warn'
      : snapshotQuery.isError
        ? 'tone-danger'
        : readiness.actionRequired > 0
          ? 'tone-warn'
          : snapshotQuery.isSuccess
            ? 'tone-ok'
            : 'tone-info'
  const StatusIcon =
    snapshotQuery.isFetching && !snapshot
      ? LoaderCircleIcon
      : statusTone === 'tone-ok'
        ? CheckCircle2Icon
        : statusTone === 'tone-warn'
          ? AlertCircleIcon
          : XCircleIcon

  const reloadSnapshot = async () => {
    const result = await snapshotQuery.refetch()
    return result.data
  }

  const onSurfaceKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const nextSurface = event.key === 'ArrowLeft' || event.key === 'Home' ? 'guided' : 'advanced'
    setSurface(nextSurface)
    document.getElementById(`settings-tab-${nextSurface}`)?.focus()
  }

  return (
    <div className="settings-workspace">
      <header className="settings-stage__header">
        <div className="settings-stage__title-block">
          <span className="t-label">Control · Settings</span>
          <h1 className="t-display">Agent settings</h1>
          <p className="settings-stage__subtitle">
            Choose the model, routing, and tools this agent can use.
          </p>
        </div>

        <div className="settings-stage__actions">
          <span className={`settings-health ${statusTone}`} role="status">
            <StatusIcon
              className={snapshotQuery.isFetching && !snapshot ? 'settings-spin' : ''}
              aria-hidden="true"
            />
            {statusLabel}
          </span>
          <Button
            type="button"
            variant="outline"
            className="text-xs uppercase tracking-[0.14em]"
            title="Refresh agent state"
            aria-label="Refresh agent state"
            aria-busy={snapshotQuery.isFetching}
            disabled={snapshotQuery.isFetching}
            onClick={() => void reloadSnapshot()}
          >
            <RefreshCwIcon className={snapshotQuery.isFetching ? 'settings-spin' : ''} />
            <span>{snapshotQuery.isFetching ? 'Refreshing…' : 'Refresh'}</span>
          </Button>
        </div>
      </header>

      <section className="settings-toolbar" aria-label="Settings workspace">
        <nav className="settings-surface-tabs" aria-label="Settings mode" role="tablist">
          <button
            id="settings-tab-guided"
            type="button"
            role="tab"
            className={surface === 'guided' ? 'is-active' : ''}
            aria-selected={surface === 'guided'}
            aria-controls="settings-panel-guided"
            tabIndex={surface === 'guided' ? 0 : -1}
            onClick={() => setSurface('guided')}
            onKeyDown={onSurfaceKeyDown}
          >
            <SlidersHorizontalIcon aria-hidden="true" />
            <span>
              <strong>Guided setup</strong>
              <small>Recommended</small>
            </span>
          </button>
          <button
            id="settings-tab-advanced"
            type="button"
            role="tab"
            className={surface === 'advanced' ? 'is-active' : ''}
            aria-selected={surface === 'advanced'}
            aria-controls="settings-panel-advanced"
            tabIndex={surface === 'advanced' ? 0 : -1}
            onClick={() => setSurface('advanced')}
            onKeyDown={onSurfaceKeyDown}
          >
            <FileCode2Icon aria-hidden="true" />
            <span>
              <strong>Advanced</strong>
              <small>Edit config</small>
            </span>
          </button>
        </nav>

        <div className="settings-glance" aria-label="Current agent setup">
          <div className="settings-glance__item">
            <CheckCircle2Icon aria-hidden="true" />
            <span>
              <small>Setup progress</small>
              <strong>{readinessLabel}</strong>
            </span>
          </div>
          <div className="settings-glance__item">
            <BotIcon aria-hidden="true" />
            <span>
              <small>{activeProvider}</small>
              <strong title={activeModel}>{activeModel}</strong>
            </span>
          </div>
        </div>
      </section>

      {snapshot?.diskDiverged ? (
        <div className="settings-load-error tone-danger" role="alert">
          <span>
            The config file changed outside AgentOS. Writes are blocked until the gateway reloads or
            restarts with that file.
          </span>
          <Button type="button" size="sm" variant="outline" onClick={() => void reloadSnapshot()}>
            Refresh state
          </Button>
        </div>
      ) : null}

      {snapshotQuery.isError ? (
        <div className="settings-load-error tone-danger" role="alert">
          <span>
            Agent state could not be loaded. Retry before changing guided or advanced settings.
          </span>
          <Button type="button" size="sm" variant="outline" onClick={() => void reloadSnapshot()}>
            Retry
          </Button>
        </div>
      ) : null}

      <section
        id="settings-panel-guided"
        className="settings-surface"
        role="tabpanel"
        aria-labelledby="settings-tab-guided"
        hidden={surface !== 'guided'}
        tabIndex={0}
      >
        {!snapshotUnavailable ? (
          <SetupPage
            embedded
            externalSnapshot={snapshot ?? null}
            onSnapshotReload={reloadSnapshot}
          />
        ) : null}
      </section>
      <section
        id="settings-panel-advanced"
        className="settings-surface"
        role="tabpanel"
        aria-labelledby="settings-tab-advanced"
        hidden={surface !== 'advanced'}
        tabIndex={0}
      >
        {!snapshotUnavailable ? (
          <ConfigPage
            embedded
            externalSnapshot={snapshot ?? null}
            externalSnapshotError={snapshotUnavailable}
            onSnapshotReload={reloadSnapshot}
          />
        ) : null}
      </section>
    </div>
  )
}
