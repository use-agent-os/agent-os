import './approvals.css'
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CheckIcon, RefreshCwIcon, ShieldAlertIcon, XIcon } from 'lucide-react'
import { toast } from 'sonner'
import { CommandLine } from '@/components/CommandLine'
import { Button } from '@/components/ui/button'
import { useBootstrap, useRpc } from '@/app/providers'
import {
  approvalCommand,
  approvalMonitor,
  canAlwaysAllow,
  saveApprovalMode,
  useApprovals,
  type Approval,
} from '@/services/approval-monitor'
import {
  MODE_OPTIONS,
  activeModeOption,
  approvalCardDetail,
  modeStateTone,
  resolveExecutionMode,
} from './logic'

// approvals.js:184 — the config.get payload carries permissions.default_mode.
interface ConfigGetResponse {
  permissions?: { default_mode?: string }
}

// approvals.js:249-281 — one pending approval card.
function ApprovalCard({
  item,
  busy,
  onResolve,
}: {
  item: Approval
  busy: boolean
  onResolve: (item: Approval, action: 'once' | 'always' | 'bypass' | 'deny') => void
}) {
  const toolName = item.toolName || item.actionKind || 'Unknown'
  const command = approvalCommand(item)
  // Card renders the FULL args JSON (approvals.js:314-322), not the modal's
  // 900-char-truncated approvalDetail(); see approvalCardDetail in ./logic.
  const detail = approvalCardDetail(item)
  const showAlways = canAlwaysAllow(item)
  return (
    <article className="ap-card tone-warn" aria-label={`Approval request ${toolName}`}>
      <header className="ap-card__head">
        <div className="ap-card__title-row">
          <span className="ap-card__name">{toolName}</span>
          {item.namespace ? <span className="ap-card__ns t-label">{item.namespace}</span> : null}
        </div>
        <span className="ap-card__time t-label">awaiting decision</span>
      </header>
      {item.agent || item.sessionKey ? (
        <div className="ap-card__meta t-data">
          {item.agent ? (
            <span>
              <b>Agent</b> {item.agent}
            </span>
          ) : null}
          {item.sessionKey ? (
            <span>
              <b>Session</b> <code>{item.sessionKey}</code>
            </span>
          ) : null}
        </div>
      ) : null}
      {command ? (
        <div className="ap-card__block">
          <div className="ap-card__block-label t-label">Command</div>
          <CommandLine command={command} toastIdPrefix="approvals-copy" />
        </div>
      ) : null}
      {detail ? (
        <div className="ap-card__block">
          <div className="ap-card__block-label t-label">Details</div>
          <pre className="ap-card__pre">{detail}</pre>
        </div>
      ) : null}
      <div className="ap-card__actions">
        <Button type="button" disabled={busy} onClick={() => onResolve(item, 'once')}>
          <CheckIcon />
          <span>Approve once</span>
        </Button>
        {showAlways ? (
          <Button
            type="button"
            variant="outline"
            disabled={busy}
            onClick={() => onResolve(item, 'always')}
          >
            Always allow this type
          </Button>
        ) : null}
        <Button
          type="button"
          variant="outline"
          disabled={busy}
          title="Bypass approval prompts while keeping sensitive-path checks"
          onClick={() => onResolve(item, 'bypass')}
        >
          Bypass approvals
        </Button>
        <Button
          type="button"
          variant="destructive"
          disabled={busy}
          onClick={() => onResolve(item, 'deny')}
        >
          <XIcon />
          <span>Deny</span>
        </Button>
      </div>
    </article>
  )
}

export function ApprovalsPage() {
  const rpc = useRpc()
  useBootstrap()
  const pending = useApprovals((s) => s.pending)
  const storeMode = useApprovals((s) => s.mode)
  // Reactive browser elevated mode: the readout re-renders when an in-view
  // Bypass resolve persists it (setBrowserElevated updates this store slice),
  // not only on a coincidental re-render that re-reads localStorage.
  const elevatedMode = useApprovals((s) => s.elevatedMode)

  useEffect(() => {
    document.title = 'Approvals - AgentOS Control'
  }, [])

  // The durable pending list + count are published by the always-running
  // approval monitor; re-poll on entry so a stale list is refreshed (legacy
  // _loadData ran on every view render, approvals.js:47).
  useEffect(() => {
    void approvalMonitor.pollNow()
  }, [])

  // approvals.js:283-303 — the strategy select is optimistic: we mirror the
  // store's mode locally so a click flips the radio immediately, then revert on
  // a failed save.
  const [pendingMode, setPendingMode] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const mode = pendingMode ?? storeMode
  const activeOpt = activeModeOption(mode)

  // approvals.js:176-192 — effective execution mode: config.get global default
  // combined with the reactive browser session elevated mode from the store.
  const configQuery = useQuery<ConfigGetResponse>({
    queryKey: ['config.get', 'approvals'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<ConfigGetResponse>('config.get')
    },
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
  })
  const globalDefaultMode = configQuery.data?.permissions?.default_mode || ''
  const execution = resolveExecutionMode(elevatedMode, globalDefaultMode)

  async function onSelectMode(next: string): Promise<void> {
    if (next === mode) return
    setPendingMode(next) // optimistic
    try {
      await saveApprovalMode(next)
      // approvals.js:297-299 — outcome toast (warn for auto-approve, else info)
      // + re-poll the monitor.
      const opts = { id: 'approvals-strategy', duration: 2500 }
      const message = 'Approval strategy: ' + next
      if (next === 'auto-approve') toast.warning(message, opts)
      else toast.success(message, opts)
      await approvalMonitor.pollNow()
    } catch (err) {
      // approvals.js:301 — revert the optimistic selection on failure.
      setPendingMode(null)
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to save strategy: ' + message, {
        id: 'approvals-strategy-err',
        duration: 4000,
      })
    }
  }

  async function onResolve(
    item: Approval,
    action: 'once' | 'always' | 'bypass' | 'deny',
  ): Promise<void> {
    if (busy) return
    setBusy(true)
    try {
      await approvalMonitor.resolve(item, action)
    } catch {
      // resolve() already toasted the failure; the monitor re-polls itself.
    } finally {
      setBusy(false)
    }
  }

  async function onRefresh(): Promise<void> {
    if (refreshing) return
    setRefreshing(true)
    try {
      await Promise.all([approvalMonitor.pollNow(), configQuery.refetch()])
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <div className="ap-stage">
      <header className="ap-stage__header">
        <div className="ap-stage__title-block">
          <span className="t-label">Control · Approvals</span>
          <h1 className="t-display">Approvals</h1>
          <p className="ap-stage__subtitle">
            Tool execution gate — keep risky actions paused until you say go.
          </p>
        </div>
        <Button
          variant="outline"
          title="Refresh approvals"
          className="ap-stage__refresh text-xs uppercase tracking-[0.14em]"
          disabled={refreshing}
          onClick={() => void onRefresh()}
        >
          <RefreshCwIcon className={refreshing ? 'ap-refresh-spin' : undefined} />
          <span>{refreshing ? 'Refreshing…' : 'Refresh'}</span>
        </Button>
      </header>

      <section className="ap-command" aria-label="Approval operations">
        <div className="ap-command__toolbar">
          <div className="ap-command__heading">
            <span className="ap-command__icon" aria-hidden="true">
              <ShieldAlertIcon />
            </span>
            <div>
              <span className="t-label">Execution gate</span>
              <strong>Decision posture</strong>
            </div>
          </div>
          <span className="ap-command__meta">
            <span className={pending.length ? 'tone-warn' : 'tone-ok'} aria-hidden="true" />
            {pending.length ? `${pending.length} waiting` : 'Queue clear'}
          </span>
        </div>
        <div className="ap-stats" aria-label="Approvals summary">
          <div className={`ap-stat ap-stat--hero tone-${modeStateTone(mode)}`}>
            <span className="ap-stat__label t-label">Pending</span>
            <strong className="ap-stat__value t-data">{pending.length}</strong>
            <span className="ap-stat__hint">
              {pending.length ? 'awaiting decision' : 'all clear'}
            </span>
          </div>
          <div className="ap-stat">
            <span className="ap-stat__label t-label">Strategy</span>
            <strong className="ap-stat__value">{activeOpt.label}</strong>
            <span className="ap-stat__hint">{activeOpt.desc}</span>
          </div>
          <div className="ap-stat">
            <span className="ap-stat__label t-label">Effective execution mode</span>
            <strong className="ap-stat__value t-data">{execution.label}</strong>
            <span className="ap-stat__hint">{execution.desc}</span>
          </div>
        </div>
      </section>

      <div className="ap-workspace">
        {pending.length === 0 ? (
          <section className="ap-empty">
            <div className="ap-empty__signal" aria-hidden="true">
              <CheckIcon />
            </div>
            <div className="ap-empty__title">No pending approvals.</div>
            <p className="ap-empty__text">
              When an agent reaches a risky tool call, it will appear here for your sign-off.
            </p>
          </section>
        ) : (
          <section className="ap-pending" aria-label="Pending approvals">
            <header className="ap-pending__head">
              <div>
                <h2>Decision inbox</h2>
                <p>Review the requested operation and choose the narrowest safe permission.</p>
              </div>
              <span className="ap-pending__count t-data">{pending.length} pending</span>
            </header>
            <div className="ap-pending__list">
              {pending.map((item) => (
                <ApprovalCard key={item.id} item={item} busy={busy} onResolve={onResolve} />
              ))}
            </div>
          </section>
        )}

        <section className="ap-strategy panel">
          <div className="panel__head">
            <div>
              <span>Approval policy</span>
              <small>Default response for future requests</small>
            </div>
          </div>
          <div className="panel__body">
            <div className="ap-strategy__options" role="radiogroup" aria-label="Approval strategy">
              {MODE_OPTIONS.map((opt) => (
                <label
                  key={opt.value}
                  className={`ap-radio${opt.value === mode ? ' is-active' : ''} tone-${modeStateTone(opt.value)}`}
                >
                  <input
                    type="radio"
                    name="ap-mode"
                    value={opt.value}
                    checked={opt.value === mode}
                    disabled={busy}
                    onChange={() => void onSelectMode(opt.value)}
                  />
                  <span className="ap-radio__indicator" aria-hidden="true" />
                  <span className="ap-radio__body">
                    <span className="ap-radio__label">{opt.label}</span>
                    <span className="ap-radio__desc">{opt.desc}</span>
                  </span>
                </label>
              ))}
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
