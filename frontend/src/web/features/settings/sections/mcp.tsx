import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CheckCircle2,
  ExternalLink,
  Eye,
  EyeOff,
  KeyRound,
  Link2,
  LogOut,
  Pencil,
  Plus,
  RefreshCw,
  Server,
  Terminal,
  Trash2,
  TriangleAlert,
  Unplug,
  XCircle,
} from 'lucide-react'
import { useId, useState } from 'react'
import { useLocation } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import robinhoodSymbolUrl from '@/assets/robinhood-symbol.png'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webSetCapabilities'
import { controlPath } from '@/lib/control-base'
import {
  blockedBecause,
  gate,
  requireConnection,
  type Gate,
  type GateBlock,
} from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import { configValueAt } from '@/views/config/logic'
import {
  createServerDraft,
  ROBINHOOD_HELP_URL,
  ROBINHOOD_MCP_URL,
  robinhoodPresentation,
  serverDetail,
  serverFromDraft,
  serverPresentation,
  transportLabel,
  validateServerDraft,
  type McpDraftErrors,
  type McpServerConfig,
  type McpServerDraft,
  type McpServerStatus,
  type McpServerTone,
  type McpStatusResponse,
  type McpTransport,
} from '@/views/mcp/logic'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/web/components/ui/alert-dialog'
import { Button, buttonVariants } from '@/web/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { Switch } from '@/web/components/ui/switch'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useConfigEditor, useConfigSnapshot, type ConfigEditor } from '../config-editor'
import { ConfigField, useRegisterFields, useReveal } from '../fields'
import { SaveBar } from '../SaveBar'
import type { McpOAuthOutcome } from '../McpOAuthCallback'

// Settings › MCP (control-ui-settings "MCP"). The runtime switch and the server
// list are configuration (`mcp.enabled`, `mcp.servers`) and go through the
// explicit-save editor; connecting, disconnecting and OAuth act on the running
// gateway immediately. OAuth returns through `/mcp/oauth/callback`, which hands
// its outcome back here in navigation state.

/** The browser hop to an authorization server; a seam so tests can observe it. */
export const oauthRedirect = {
  assign(url: string): void {
    window.location.assign(url)
  },
}

interface ConnectResponse {
  connected?: boolean
  authorizationRequired?: boolean
  authorizationUrl?: string
}

const STATUS_KEY = ['mcp', 'status'] as const

const TONE_CLASS: Record<McpServerTone, string> = {
  connected: 'border-ok/40 text-ok',
  authorization: 'border-warn/50 text-warn',
  paused: 'border-border text-text-muted',
  offline: 'border-border text-text-muted',
  unavailable: 'border-danger/40 text-danger',
}

const inputClass =
  'h-9 w-full rounded-md border border-border-control bg-surface-0 px-3 aria-[invalid=true]:border-danger touch:h-11 touch:text-base'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function asServers(value: unknown): McpServerConfig[] {
  return Array.isArray(value) ? (value as McpServerConfig[]) : []
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

function useMcpStatus() {
  const rpc = useRpc()
  return useQuery({
    queryKey: STATUS_KEY,
    retry: false,
    queryFn: async () => {
      await rpc.waitForConnection()
      return (await rpc.call<McpStatusResponse>('mcp.status')) ?? {}
    },
  })
}

/**
 * The section's editor, with the follow-up a saved server list needs: forget
 * the OAuth tokens of removed or renamed servers, disconnect everything when
 * the runtime is paused, and connect the servers that were added or changed.
 */
function useMcpEditor(): ConfigEditor {
  const editor = useConfigEditor()
  const rpc = useRpc()
  const queryClient = useQueryClient()

  const sync = async (saved: () => Promise<boolean>) => {
    const before = asServers(configValueAt(editor.config, 'mcp.servers'))
    const after = asServers(editor.value('mcp.servers'))
    const enabled = Boolean(editor.value('mcp.enabled'))
    if (!(await saved())) return false
    const kept = new Set(after.map((s) => s.name))
    const settle = (calls: Promise<unknown>[]) => Promise.allSettled(calls)
    await settle(
      before
        .filter((s) => !kept.has(s.name))
        .map((s) =>
          rpc
            .call('mcp.oauth.clear', { name: s.name })
            .catch(() => rpc.call('mcp.disconnect', { name: s.name })),
        ),
    )
    if (!enabled) {
      await settle(after.map((s) => rpc.call('mcp.disconnect', { name: s.name })))
    } else {
      const changed = after.filter(
        (s) =>
          !same(
            s,
            before.find((b) => b.name === s.name),
          ),
      )
      await settle(changed.map((s) => rpc.call('mcp.connect', { name: s.name })))
    }
    await queryClient.invalidateQueries({ queryKey: STATUS_KEY })
    return true
  }
  return {
    ...editor,
    save: () => sync(editor.save),
    overwrite: () => sync(editor.overwrite),
  }
}

export function McpSection() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const editor = useMcpEditor()
  const snapshot = useConfigSnapshot()
  const status = useMcpStatus()
  const connection = useDisplayedConnection()
  const offline = requireConnection(connection)
  const online = gate(offline)
  const reasonId = useId()
  const location = useLocation()
  const outcome = (location.state as Partial<McpOAuthOutcome> | null)?.mcpOAuth
  const [outcomeDismissed, setOutcomeDismissed] = useState(false)

  const [draft, setDraft] = useState<McpServerDraft | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})

  useRegisterFields('mcp', [
    { id: 'mcp.enabled', label: t('webSetCapabilities.mcpRuntime') },
    { id: 'mcp-servers', label: t('webSetCapabilities.mcpServers') },
  ])

  const enabled = Boolean(editor.value('mcp.enabled'))
  const servers = asServers(editor.value('mcp.servers'))
  const saved = asServers(configValueAt(editor.config, 'mcp.servers'))
  const statusByName: Record<string, McpServerStatus> = Object.fromEntries(
    (status.data?.servers ?? []).map((s) => [s.name, s]),
  )
  const statusAvailable = !status.isError
  const connected = Object.values(statusByName).filter((s) => s.connected).length
  const tools = Object.values(statusByName).reduce((n, s) => n + (s.tools?.length ?? 0), 0)
  const savedEnabled = Boolean(configValueAt(editor.config, 'mcp.enabled'))

  const refresh = () => queryClient.invalidateQueries({ queryKey: STATUS_KEY })

  /** Run a row action, keeping its failure next to the row. */
  const act = async (name: string, kind: string, work: () => Promise<void>) => {
    setBusy(`${kind}:${name}`)
    setRowErrors((prev) => {
      const next = { ...prev }
      delete next[name]
      return next
    })
    try {
      await work()
    } catch (error) {
      setRowErrors((prev) => ({ ...prev, [name]: errorMessage(error) }))
    } finally {
      setBusy(null)
      void refresh()
    }
  }

  const startOAuth = async (name: string) => {
    const result = await rpc.call<ConnectResponse>('mcp.oauth.start', {
      name,
      redirectUri: `${window.location.origin}${controlPath('/mcp/oauth/callback')}`,
    })
    if (result?.connected) {
      toast.success(t('webSetCapabilities.mcpConnected', { name }))
      return
    }
    if (!result?.authorizationUrl) throw new Error(t('webSetCapabilities.mcpNoAuthUrl'))
    oauthRedirect.assign(result.authorizationUrl)
  }

  const connect = (name: string) =>
    act(name, 'connect', async () => {
      const result = await rpc.call<ConnectResponse>('mcp.connect', { name })
      if (result?.authorizationRequired) await startOAuth(name)
      else toast.success(t('webSetCapabilities.mcpConnected', { name }))
    })

  const authorize = (name: string) => act(name, 'connect', () => startOAuth(name))

  const disconnect = (name: string) =>
    act(name, 'disconnect', async () => {
      await rpc.call('mcp.disconnect', { name })
      toast.success(t('webSetCapabilities.mcpDisconnected', { name }))
    })

  const signOut = (name: string) =>
    act(name, 'signout', async () => {
      await rpc.call('mcp.oauth.clear', { name })
      toast.success(t('webSetCapabilities.mcpSignedOut', { name }))
    })

  const stageServer = (server: McpServerConfig, originalName: string | null) => {
    const next = [...servers]
    const index = next.findIndex((s) => s.name === originalName)
    if (index >= 0) next[index] = server
    else next.push(server)
    editor.set('mcp.servers', next)
    setDraft(null)
  }

  const openRobinhood = () => {
    const existing = servers.find((s) => s.url === ROBINHOOD_MCP_URL)
    setDraft(
      existing
        ? createServerDraft({ ...existing, originalName: existing.name })
        : createServerDraft({ name: 'robinhood-trading', url: ROBINHOOD_MCP_URL, oauth: true }),
    )
  }

  if (snapshot.isError) {
    return (
      <ErrorState
        title={t('webSetCapabilities.mcpLoadFailed')}
        message={errorMessage(snapshot.error)}
        onRetry={() => void snapshot.refetch()}
      />
    )
  }
  if (!editor.ready) {
    return (
      <LoadingRegion>
        <Skeleton className="h-40" />
      </LoadingRegion>
    )
  }

  const robinhood = robinhoodPresentation(servers, statusByName, savedEnabled, statusAvailable)

  return (
    <div className="flex flex-col gap-4">
      {outcome && !outcomeDismissed ? (
        <div
          role={outcome.ok ? 'status' : 'alert'}
          className={cn(
            'flex items-start gap-2 rounded-lg border px-3 py-2',
            outcome.ok ? 'border-ok/40 bg-ok/10' : 'border-danger/40 bg-danger/10',
          )}
        >
          {outcome.ok ? (
            <CheckCircle2 className="mt-0.5 size-4 text-ok" aria-hidden data-status-icon />
          ) : (
            <XCircle className="mt-0.5 size-4 text-danger" aria-hidden data-status-icon />
          )}
          <span className="min-w-0 flex-1">
            {outcome.ok
              ? t('webSetCapabilities.mcpOAuthDone')
              : t('webSetCapabilities.mcpOAuthError', { message: outcome.message })}
          </span>
          <Button size="sm" variant="ghost" onClick={() => setOutcomeDismissed(true)}>
            {t('webSetCapabilities.dismiss')}
          </Button>
        </div>
      ) : null}

      {online.reason ? (
        <p
          id={reasonId}
          role="status"
          className="flex items-center gap-2 rounded-md border border-warn/50 bg-warn/10 px-3 py-2 text-small"
        >
          <TriangleAlert className="size-4 shrink-0 text-warn" aria-hidden data-status-icon />
          {online.reason}
        </p>
      ) : null}

      <ConfigField
        editor={editor}
        path="mcp.enabled"
        label={t('webSetCapabilities.mcpRuntime')}
        help={t('webSetCapabilities.mcpRuntimeHelp')}
        type={{ kind: 'boolean' }}
      />

      {status.isError ? (
        <ErrorState
          title={t('webSetCapabilities.mcpStatusFailed')}
          message={t('webSetCapabilities.mcpStatusFailedBody')}
          onRetry={() => void status.refetch()}
        />
      ) : null}

      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Tile label={t('webSetCapabilities.mcpTileConfigured')} value={String(saved.length)} />
        <Tile
          label={t('webSetCapabilities.mcpTileConnected')}
          value={statusAvailable ? String(connected) : t('webSetCapabilities.dash')}
        />
        <Tile
          label={t('webSetCapabilities.mcpTileTools')}
          value={statusAvailable ? String(tools) : t('webSetCapabilities.dash')}
        />
        <Tile
          label={t('webSetCapabilities.mcpTileRuntime')}
          value={
            !statusAvailable
              ? t('webSetCapabilities.mcpRuntimeUnknown')
              : savedEnabled
                ? t('webSetCapabilities.mcpRuntimeOn')
                : t('webSetCapabilities.mcpRuntimeOff')
          }
        />
      </dl>

      <section
        aria-label={t('webSetCapabilities.mcpPartner')}
        className="flex flex-col gap-2 rounded-lg border border-border bg-surface-1 p-3"
      >
        <div className="flex items-start gap-3">
          <img
            src={robinhoodSymbolUrl}
            alt={t('webSetCapabilities.mcpRobinhoodLogo')}
            width={40}
            height={40}
            className="size-10 shrink-0 rounded-md"
          />
          <div className="min-w-0 flex-1">
            <h2 className="flex flex-wrap items-center gap-2 font-semibold">
              {t('webSetCapabilities.mcpRobinhoodTitle')}
              <span className="rounded-full border border-border px-2 py-0.5 text-small font-normal">
                {robinhood.label}
              </span>
            </h2>
            <p className="text-text-muted">{t('webSetCapabilities.mcpRobinhoodBody')}</p>
            <p className="text-small text-text-muted">
              {robinhood.detail} · {robinhood.tools}
            </p>
          </div>
        </div>
        <p role="note" className="flex items-start gap-1.5 text-small">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warn" aria-hidden />
          {t('webSetCapabilities.mcpRobinhoodNotice')}
        </p>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="primary" onClick={openRobinhood}>
            <Link2 aria-hidden />
            {robinhood.action}
          </Button>
          <a
            href={ROBINHOOD_HELP_URL}
            target="_blank"
            rel="noopener noreferrer"
            className={buttonVariants({ size: 'sm', variant: 'ghost' })}
          >
            {t('webSetCapabilities.mcpRobinhoodGuide')}
            <ExternalLink aria-hidden />
          </a>
        </div>
      </section>

      <section
        aria-labelledby="mcp-servers-title"
        data-field-id="mcp-servers"
        className="flex flex-col gap-2"
      >
        <div className="flex flex-wrap items-center gap-2">
          <h2 id="mcp-servers-title" className="font-semibold">
            {t('webSetCapabilities.mcpServers')}
          </h2>
          <span className="flex-1" />
          <Button size="sm" variant="ghost" onClick={() => void refresh()}>
            <RefreshCw aria-hidden />
            {t('webSetCapabilities.refresh')}
          </Button>
          <Button size="sm" variant="outline" onClick={() => setDraft(createServerDraft())}>
            <Plus aria-hidden />
            {t('webSetCapabilities.mcpAdd')}
          </Button>
        </div>

        {servers.length === 0 ? (
          <EmptyState
            title={t('webSetCapabilities.mcpEmpty')}
            description={t('webSetCapabilities.mcpEmptyBody')}
            className="py-8"
          />
        ) : (
          <ul role="list" className="divide-y divide-border rounded-lg border border-border">
            {servers.map((server) => (
              <li key={server.name}>
                <ServerRow
                  server={server}
                  status={statusByName[server.name]}
                  runtimeOn={savedEnabled}
                  statusAvailable={statusAvailable}
                  unsaved={
                    !same(
                      server,
                      saved.find((s) => s.name === server.name),
                    )
                  }
                  online={online}
                  offline={offline}
                  reasonId={reasonId}
                  busy={busy}
                  error={rowErrors[server.name]}
                  onConnect={() => void connect(server.name)}
                  onAuthorize={() => void authorize(server.name)}
                  onDisconnect={() => void disconnect(server.name)}
                  onSignOut={() => void signOut(server.name)}
                  onEdit={() =>
                    setDraft(createServerDraft({ ...server, originalName: server.name }))
                  }
                  onRemove={() => setRemoving(server.name)}
                />
              </li>
            ))}
          </ul>
        )}
        {!same(enabled, savedEnabled) || !same(servers, saved) ? (
          <p className="text-small text-text-muted">{t('webSetCapabilities.mcpApplyOnSave')}</p>
        ) : null}
      </section>

      <ServerDialog
        draft={draft}
        servers={servers}
        onChange={setDraft}
        onClose={() => setDraft(null)}
        onSave={stageServer}
      />

      <AlertDialog
        open={removing !== null}
        onOpenChange={(next) => (next ? undefined : setRemoving(null))}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('webSetCapabilities.mcpRemoveTitle', { name: String(removing) })}
            </AlertDialogTitle>
            <AlertDialogDescription>{t('webSetCapabilities.mcpRemoveBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webCommon.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              destructive
              onClick={() =>
                editor.set(
                  'mcp.servers',
                  servers.filter((s) => s.name !== removing),
                )
              }
            >
              {t('webSetCapabilities.mcpRemove')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <SaveBar editor={editor} />
    </div>
  )
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border px-3 py-2">
      <dt className="text-small text-text-muted">{label}</dt>
      <dd className="text-lg font-semibold">{value}</dd>
    </div>
  )
}

function ServerRow({
  server,
  status,
  runtimeOn,
  statusAvailable,
  unsaved,
  online,
  offline,
  reasonId,
  busy,
  error,
  onConnect,
  onAuthorize,
  onDisconnect,
  onSignOut,
  onEdit,
  onRemove,
}: {
  server: McpServerConfig
  status?: McpServerStatus
  runtimeOn: boolean
  statusAvailable: boolean
  unsaved: boolean
  online: Gate
  offline: GateBlock | null
  reasonId: string
  busy: string | null
  error?: string
  onConnect: () => void
  onAuthorize: () => void
  onDisconnect: () => void
  onSignOut: () => void
  onEdit: () => void
  onRemove: () => void
}) {
  const state = serverPresentation(server, status, runtimeOn, statusAvailable)
  const needsAuth = server.oauth && !status?.authenticated
  // Connecting acts on the saved configuration, so an unsaved edit blocks it.
  const run = gate(offline, unsaved && blockedBecause(t('webSetCapabilities.mcpSaveFirst')))
  const rowReasonId = `${reasonId}-${server.name}`
  const describedBy = run.reason ? (online.disabled ? reasonId : rowReasonId) : undefined
  const rowBusy = busy?.endsWith(`:${server.name}`) ?? false

  return (
    <div className="flex flex-col gap-1 px-3 py-2">
      <div className="flex flex-wrap items-start gap-3">
        <span className="mt-0.5 text-text-muted" aria-hidden>
          {server.transport === 'stdio' ? (
            <Terminal className="size-4" />
          ) : (
            <Server className="size-4" />
          )}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{server.name}</span>
            <span
              title={state.detail}
              className={cn('rounded-full border px-2 py-0.5 text-small', TONE_CLASS[state.tone])}
            >
              {state.label}
            </span>
            {unsaved ? (
              <span className="rounded-full border border-warn/50 px-2 py-0.5 text-small text-warn">
                {t('webSetCapabilities.mcpUnsaved')}
              </span>
            ) : null}
          </div>
          <p className="text-small text-text-muted">
            {transportLabel(server.transport)}
            {server.oauth ? ` · ${t('webSetCapabilities.mcpOAuth')}` : ''}
            {state.toolCount ? ` · ${tPlural('webSetCapabilities.mcpTools', state.toolCount)}` : ''}
          </p>
          <code className="block truncate text-small text-text-muted" title={serverDetail(server)}>
            {serverDetail(server)}
          </code>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {statusAvailable && runtimeOn && !status?.connected ? (
            <Button
              size="sm"
              variant="primary"
              disabled={run.disabled || rowBusy}
              aria-describedby={describedBy}
              onClick={needsAuth ? onAuthorize : onConnect}
            >
              {needsAuth ? <KeyRound aria-hidden /> : <Link2 aria-hidden />}
              {rowBusy
                ? t('webSetCapabilities.mcpConnecting')
                : needsAuth
                  ? t('webSetCapabilities.mcpAuthorize')
                  : t('webSetCapabilities.mcpConnect')}
            </Button>
          ) : null}
          {status?.connected ? (
            <Button
              size="sm"
              variant="outline"
              disabled={online.disabled || rowBusy}
              aria-describedby={online.reason ? reasonId : undefined}
              onClick={onDisconnect}
            >
              <Unplug aria-hidden />
              {t('webSetCapabilities.mcpDisconnect')}
            </Button>
          ) : null}
          {server.oauth && status?.authenticated ? (
            <Button
              size="sm"
              variant="ghost"
              disabled={online.disabled || rowBusy}
              aria-describedby={online.reason ? reasonId : undefined}
              onClick={onSignOut}
            >
              <LogOut aria-hidden />
              {t('webSetCapabilities.mcpSignOut')}
            </Button>
          ) : null}
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={t('webSetCapabilities.mcpEdit', { name: server.name })}
            onClick={onEdit}
          >
            <Pencil />
          </Button>
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={t('webSetCapabilities.mcpRemoveNamed', { name: server.name })}
            onClick={onRemove}
          >
            <Trash2 />
          </Button>
        </div>
      </div>
      {run.reason && !online.disabled && statusAvailable && runtimeOn && !status?.connected ? (
        <p id={rowReasonId} className="text-small text-text-muted">
          {run.reason}
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="text-small text-danger">
          {error}
        </p>
      ) : null}
    </div>
  )
}

function ServerDialog({
  draft,
  servers,
  onChange,
  onClose,
  onSave,
}: {
  draft: McpServerDraft | null
  servers: McpServerConfig[]
  onChange: (draft: McpServerDraft) => void
  onClose: () => void
  onSave: (server: McpServerConfig, originalName: string | null) => void
}) {
  return (
    <Dialog open={draft !== null} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="max-w-lg">
        {draft ? (
          <ServerForm
            draft={draft}
            servers={servers}
            onChange={onChange}
            onClose={onClose}
            onSave={onSave}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

function Field({
  label,
  help,
  error,
  children,
  id,
}: {
  label: string
  help?: string
  error?: string
  children: React.ReactNode
  id: string
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="font-medium">
        {label}
      </label>
      {children}
      {help ? (
        <p id={`${id}-help`} className="text-small text-text-muted">
          {help}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} role="alert" className="text-small text-danger">
          {error}
        </p>
      ) : null}
    </div>
  )
}

function ServerForm({
  draft,
  servers,
  onChange,
  onClose,
  onSave,
}: {
  draft: McpServerDraft
  servers: McpServerConfig[]
  onChange: (draft: McpServerDraft) => void
  onClose: () => void
  onSave: (server: McpServerConfig, originalName: string | null) => void
}) {
  const id = useId()
  const [errors, setErrors] = useState<McpDraftErrors>({})
  const [headersShown, toggleHeaders] = useReveal()
  const http = draft.transport !== 'stdio'

  const update = <K extends keyof McpServerDraft>(key: K, value: McpServerDraft[K]) => {
    onChange({ ...draft, [key]: value })
    setErrors((prev) => ({ ...prev, [key]: undefined }))
  }
  const described = (key: string, help: boolean, error?: string) =>
    [help ? `${id}-${key}-help` : '', error ? `${id}-${key}-error` : '']
      .filter(Boolean)
      .join(' ') || undefined

  return (
    <form
      noValidate
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault()
        const next = validateServerDraft(draft, servers)
        setErrors(next)
        if (Object.values(next).some(Boolean)) return
        onSave(serverFromDraft(draft), draft.originalName)
      }}
    >
      <DialogHeader>
        <DialogTitle>
          {draft.originalName
            ? t('webSetCapabilities.mcpEditTitle')
            : t('webSetCapabilities.mcpAddTitle')}
        </DialogTitle>
        <DialogDescription>{t('webSetCapabilities.mcpDialogBody')}</DialogDescription>
      </DialogHeader>

      <Field
        id={`${id}-name`}
        label={t('webSetCapabilities.mcpFieldName')}
        help={t('webSetCapabilities.mcpFieldNameHelp')}
        error={errors.name}
      >
        <input
          id={`${id}-name`}
          className={inputClass}
          value={draft.name}
          maxLength={64}
          autoComplete="off"
          aria-invalid={errors.name ? true : undefined}
          aria-describedby={described('name', true, errors.name)}
          onChange={(e) => update('name', e.target.value)}
        />
      </Field>

      <Field id={`${id}-transport`} label={t('webSetCapabilities.mcpFieldTransport')}>
        <select
          id={`${id}-transport`}
          className={inputClass}
          value={draft.transport}
          onChange={(e) => {
            const transport = e.target.value as McpTransport
            onChange({
              ...draft,
              transport,
              oauth: transport === 'streamable_http' ? draft.oauth : false,
            })
          }}
        >
          {(['streamable_http', 'sse', 'stdio'] as const).map((value) => (
            <option key={value} value={value}>
              {transportLabel(value)}
            </option>
          ))}
        </select>
      </Field>

      {http ? (
        <>
          <Field id={`${id}-url`} label={t('webSetCapabilities.mcpFieldUrl')} error={errors.url}>
            <input
              id={`${id}-url`}
              type="url"
              className={inputClass}
              value={draft.url}
              autoComplete="url"
              placeholder={t('webSetCapabilities.mcpFieldUrlPlaceholder')}
              aria-invalid={errors.url ? true : undefined}
              aria-describedby={described('url', false, errors.url)}
              onChange={(e) => update('url', e.target.value)}
            />
          </Field>
          {draft.transport === 'streamable_http' ? (
            <div className="flex items-start gap-3">
              <Switch
                id={`${id}-oauth`}
                checked={draft.oauth}
                aria-describedby={`${id}-oauth-help`}
                onCheckedChange={(v) => update('oauth', v)}
              />
              <div>
                <label htmlFor={`${id}-oauth`} className="font-medium">
                  {t('webSetCapabilities.mcpFieldOAuth')}
                </label>
                <p id={`${id}-oauth-help`} className="text-small text-text-muted">
                  {t('webSetCapabilities.mcpFieldOAuthHelp')}
                </p>
              </div>
            </div>
          ) : null}
          <Field
            id={`${id}-headers`}
            label={t('webSetCapabilities.mcpFieldHeaders')}
            help={t('webSetCapabilities.mcpFieldHeadersHelp')}
            error={errors.headers}
          >
            <div className="flex items-start gap-2">
              {/* Headers usually carry credentials, so they stay masked until revealed. */}
              <textarea
                id={`${id}-headers`}
                rows={4}
                spellCheck={false}
                className={cn(
                  inputClass,
                  'h-auto py-2 font-mono text-small',
                  !headersShown && '[-webkit-text-security:disc]',
                )}
                value={draft.headers}
                aria-invalid={errors.headers ? true : undefined}
                aria-describedby={described('headers', true, errors.headers)}
                onChange={(e) => update('headers', e.target.value)}
              />
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={
                  headersShown ? t('webSetCapabilities.hide') : t('webSetCapabilities.reveal')
                }
                onClick={toggleHeaders}
              >
                {headersShown ? <EyeOff /> : <Eye />}
              </Button>
            </div>
          </Field>
        </>
      ) : (
        <>
          <Field
            id={`${id}-command`}
            label={t('webSetCapabilities.mcpFieldCommand')}
            error={errors.command}
          >
            <input
              id={`${id}-command`}
              className={inputClass}
              value={draft.command}
              autoComplete="off"
              placeholder={t('webSetCapabilities.mcpFieldCommandPlaceholder')}
              aria-invalid={errors.command ? true : undefined}
              aria-describedby={described('command', false, errors.command)}
              onChange={(e) => update('command', e.target.value)}
            />
          </Field>
          <Field
            id={`${id}-args`}
            label={t('webSetCapabilities.mcpFieldArgs')}
            help={t('webSetCapabilities.mcpFieldArgsHelp')}
          >
            <input
              id={`${id}-args`}
              className={inputClass}
              value={draft.args}
              autoComplete="off"
              aria-describedby={`${id}-args-help`}
              onChange={(e) => update('args', e.target.value)}
            />
          </Field>
        </>
      )}

      <Field
        id={`${id}-timeout`}
        label={t('webSetCapabilities.mcpFieldTimeout')}
        error={errors.timeout}
      >
        <input
          id={`${id}-timeout`}
          type="number"
          min={1}
          max={600}
          className={cn(inputClass, 'max-w-32')}
          value={draft.timeout}
          aria-invalid={errors.timeout ? true : undefined}
          aria-describedby={described('timeout', false, errors.timeout)}
          onChange={(e) => update('timeout', e.target.value)}
        />
      </Field>

      <DialogFooter>
        <Button variant="ghost" onClick={onClose}>
          {t('webCommon.cancel')}
        </Button>
        <Button type="submit" variant="primary">
          {t('webSetCapabilities.mcpStage')}
        </Button>
      </DialogFooter>
    </form>
  )
}
