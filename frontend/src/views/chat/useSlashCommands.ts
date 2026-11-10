import { t } from '@/i18n'
import '@/i18n/en/chat'

import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import {
  normalizeSlashCommand,
  slashCommandKey,
  watchGatewayReconnect,
  type SlashCommand,
} from './logic'
import { resetSession } from './resetSession'

/**
 * Slash-command catalog + execution (React).
 *
 * the catalog load
 * `_loadSlashCommands` (, RPC `commands.list_for_surface` with
 * `{ surface: 'web_chat' }`), the command map, and the
 * dispatch `_executeSlashCommand` → `_selectSlashCmd` .
 *
 * The catalog + the command map live here (one source) so both <SlashMenu> (which
 * renders/filters) and the composer send path (which executes on Enter over a
 * typed `/cmd`) share them — DRY, and avoids two RPC loads.
 *
 * `_selectSlashCmd`'s action switch dispatches on the
 * serialized `execution.action` / `rpc_method`. The RPC-backed branches (reset /
 * usage / model / router.hold.set / router.hold.clear) are ported faithfully as
 * `rpc.call(...) + toast`, and `plan.mode.set` (newer than that switch) takes
 * the same shape. The two branches that cross into session/stream
 * ownership delegate through `onSessionAction`: `new_chat`
 * switches/persists/re-subscribes, while `compact_context`
 * uses the composed compaction controller for in-flight UI around the RPC.
 */

export interface UseSlashCommands {
  /** The normalized catalog. */
  commands: SlashCommand[]
  /**
   * Execute a typed slash-command line.
   * If the catalog hasn't loaded yet, awaits the same load the mount effect
   * kicked off (`if (!_slashCatalogLoaded) await
   * _loadSlashCommands;`) before looking the command up — this covers a
   * user submitting a command before the catalog RPC has resolved. Then looks
   * the command up in the map, splits off args, and dispatches. Always
   * resolves true (so the caller does NOT also send the text as a chat
   * message) — including for an unknown command, which legacy toasts a
   * warning for but still swallows rather than sending as text.
   */
  execute: (text: string) => Promise<boolean>
  /**
   * True when the line's first word names a command in the catalog (loading
   * it first if needed). A caller that sends unknown `/…` text as a message
   * asks this before `execute`.
   */
  isCommand: (text: string) => Promise<boolean>
}

/** `/model` without a transcript: how many rows its toast lists, and for how long. */
const TOAST_MODEL_LINES = 12
const TOAST_MODEL_MS = 10_000

export function useSlashCommands(opts?: {
  sessionKey: string
  /** Delegate the session/stream-mutating actions (new_chat / compact) to the
   * transcript owner. Receives the resolved action + the command + args. */
  onSessionAction?: (action: string, cmd: SlashCommand, args: string) => void
  /** Append a system message row (`/model` result). */
  addSystemMessage?: (text: string) => void
  /** A slash command changed the route hold (`/c0`–`/c3`, `/use`, `/auto`):
   * the composer's route chip re-reads it, since it only reads on its own. */
  onRouteHoldChange?: () => void
}): UseSlashCommands {
  const rpc = useRpc()
  // `/plan` refreshes the Toolbar's plan pill, which reads this query cache.
  const queryClient = useQueryClient()
  const sessionKey = opts?.sessionKey ?? ''
  const [commands, setCommands] = useState<SlashCommand[]>([])
  // `_slashCatalogLoaded` — set once the catalog resolves (success
  // or failure), so `execute` knows whether it must await a load
  // first. Held in a ref: read imperatively, never during render.
  const catalogLoadedRef = useRef(false)
  // In-flight load promise, shared between the mount effect and `execute` so a
  // command typed before the catalog resolves awaits the SAME RPC call rather
  // than firing a second `commands.list_for_surface`.
  const loadPromiseRef = useRef<Promise<void> | null>(null)

  // Late-bound holders so the `execute` closure always reads the latest session
  // key / delegates. Written in an effect (never during render) — `execute` is
  // called imperatively, so it does not need to be recreated when these change.
  const sessionKeyRef = useRef(sessionKey)
  const onSessionActionRef = useRef(opts?.onSessionAction)
  const addSystemMessageRef = useRef(opts?.addSystemMessage)
  const onRouteHoldChangeRef = useRef(opts?.onRouteHoldChange)
  useEffect(() => {
    sessionKeyRef.current = sessionKey
    onSessionActionRef.current = opts?.onSessionAction
    addSystemMessageRef.current = opts?.addSystemMessage
    onRouteHoldChangeRef.current = opts?.onRouteHoldChange
  }, [sessionKey, opts?.onSessionAction, opts?.addSystemMessage, opts?.onRouteHoldChange])

  // ── Catalog load ─────────────────
  // Extracted so both the mount effect below AND `execute` (
  // `if (!_slashCatalogLoaded) await _loadSlashCommands;`) can trigger the
  // same load and share one in-flight promise — a command typed before the
  // catalog resolves awaits the load rather than firing a second RPC call.
  // The command map: name + every alias → the command. Held
  // in a ref so the imperative `execute` always reads the latest synchronously.
  // `loadCatalog` writes this ref directly (not via a `useEffect` keyed off
  // `commands` state) because `execute` awaits `loadCatalog` and then reads
  // the map in the SAME async continuation — it cannot wait for a subsequent
  // render + effect flush to pick up the freshly loaded commands.
  const commandMapRef = useRef<Map<string, SlashCommand>>(new Map())

  const loadCatalog = useCallback((): Promise<void> => {
    if (loadPromiseRef.current) return loadPromiseRef.current
    const promise = (async () => {
      try {
        await rpc.waitForConnection()
        const res = (await rpc.call('commands.list_for_surface', { surface: 'web_chat' })) as {
          commands?: unknown[]
        } | null
        const list = Array.isArray(res?.commands) ? res.commands : []
        const normalized = list.map((c) => normalizeSlashCommand(c as Record<string, unknown>))
        const map = new Map<string, SlashCommand>()
        normalized.forEach((cmd) => {
          map.set(slashCommandKey(cmd.name), cmd)
          ;(cmd.aliases || []).forEach((alias) => map.set(slashCommandKey(alias), cmd))
        })
        commandMapRef.current = map
        setCommands(normalized)
        catalogLoadedRef.current = true
      } catch {
        commandMapRef.current = new Map()
        setCommands([])
        catalogLoadedRef.current = false
      } finally {
        loadPromiseRef.current = null
      }
    })()
    loadPromiseRef.current = promise
    return promise
  }, [rpc])

  useEffect(() => {
    void loadCatalog()
    // No cleanup/cancellation flag: `loadCatalog` is shared with `execute`, so
    // an unmount mid-load must not stop `execute`'s own await on it. The
    // effect only needs to kick the load off once on mount.
  }, [loadCatalog])

  // ── _selectSlashCmd action switch, RPC-backed branches ──
  const dispatch = useCallback(
    (cmd: SlashCommand, args: string) => {
      const key = sessionKeyRef.current
      const commandName = cmd?.cmd || cmd?.name || ''
      // a `/reset` command whose name is actually `/new` runs the
      // new_chat action. Resolve that remap here rather than recursing.
      let action = cmd?.execution?.action || cmd.cmd || cmd.name || ''
      if (
        (action === 'reset_session' || action === 'sessions.reset' || action === '/reset') &&
        commandName === '/new'
      ) {
        action = 'new_chat'
      }

      switch (action) {
        // delegate the fully wired React session swap.
        case 'new_chat':
        case '/new': {
          if (onSessionActionRef.current) onSessionActionRef.current('new_chat', cmd, args)
          else toast.info(t('chat.slashNewChatHint'))
          return
        }
        // reset the current session. (The `/new`-named-but-
        // reset-action remap is resolved above into `new_chat`.)
        case 'reset_session':
        case 'sessions.reset':
        case '/reset': {
          void resetSession(rpc, key)
          return
        }
        // manual compaction. The RPC is fired; the in-thread
        // separator + in-flight controls belong to the compaction controller.
        case 'compact_context':
        case 'sessions.contextCompact':
        case '/compact': {
          if (onSessionActionRef.current) {
            onSessionActionRef.current('compact_context', cmd, args)
            return
          }
          rpc
            .call('sessions.contextCompact', { key })
            .then(() => toast.info(t('chat.slashCompactionRequested')))
            .catch((err: unknown) =>
              toast.error(
                t('chat.slashCompactionFailed', {
                  message: err instanceof Error ? err.message : String(err),
                }),
              ),
            )
          return
        }
        // usage status / cost.
        case 'usage_status':
        case 'usage.status':
        case '/usage': {
          const arg = args.trim().toLowerCase()
          if (arg === 'page') {
            toast.info(t('chat.slashUsageHint'))
            return
          }
          const method = arg === 'cost' ? 'usage.cost' : 'usage.status'
          rpc
            .call(method)
            .then((result: unknown) => {
              const r = (result ?? {}) as Record<string, unknown>
              const totals = (r.totals ?? {}) as Record<string, unknown>
              if (method === 'usage.cost') {
                const total = r.totalCostUsd ?? r.total_cost_usd ?? totals.cost ?? totals.cost_usd
                toast.info(
                  total != null
                    ? `Usage cost: $${Number(total).toFixed(6)}`
                    : t('chat.slashUsageUnavailable'),
                )
                return
              }
              const tokens = Number(
                r.totalTokens ??
                  r.total_tokens ??
                  totals.tokens ??
                  totals.total_tokens ??
                  totals.totalTokens ??
                  0,
              )
              const cost = r.totalCostUsd ?? r.total_cost_usd ?? totals.cost ?? totals.cost_usd
              toast.info(
                `Usage: ${tokens.toLocaleString()} tokens` +
                  (cost != null ? ` · $${Number(cost).toFixed(6)}` : ''),
              )
            })
            .catch((err: unknown) =>
              toast.error(
                t('chat.slashUsageFailed', {
                  message: err instanceof Error ? err.message : String(err),
                }),
              ),
            )
          return
        }
        // `/plan [off]` — plan mode on, or off with `off` (also `exit` / `stop`,
        // as the gateway's own parser reads it for text surfaces). The flag
        // lives in gateway memory; the Toolbar pill reads it back through
        // `plan.mode.get`, so that query is refreshed either way.
        case 'plan.mode.set':
        case '/plan': {
          const off = ['off', 'exit', 'stop'].includes(args.trim().toLowerCase())
          rpc
            .call('plan.mode.set', { key, mode: off ? 'off' : 'on' })
            .then((res: unknown) => {
              const on = (res as { planMode?: boolean } | null)?.planMode ?? !off
              toast.info(on ? t('chat.slashPlanOn') : t('chat.slashPlanOff'))
            })
            .catch((err: unknown) =>
              toast.error(
                t('chat.slashPlanFailed', {
                  message: err instanceof Error ? err.message : String(err),
                }),
              ),
            )
            .finally(() => void queryClient.invalidateQueries({ queryKey: ['plan.mode.get', key] }))
          return
        }
        // model list (optionally filtered), into a system row.
        case 'models.list':
        case '/model': {
          const filter = args.trim().toLowerCase()
          rpc
            .call('models.list', {})
            .then((models: unknown) => {
              const list = Array.isArray(models) ? (models as Record<string, unknown>[]) : []
              const matches = filter
                ? list.filter((m) =>
                    [m.id, m.name, m.provider].some((v) =>
                      String(v || '')
                        .toLowerCase()
                        .includes(filter),
                    ),
                  )
                : list
              if (matches.length === 0) {
                toast.info(
                  filter ? t('chat.slashNoModelsMatch', { filter }) : t('chat.slashNoModels'),
                )
                return
              }
              const lines = matches.map((m) => {
                const ctx =
                  Number(m.contextWindow) > 0
                    ? ` · ${Math.round(Number(m.contextWindow) / 1000)}k ctx`
                    : ''
                return `• ${m.name || m.id} (${m.id}) — ${m.provider || 'unknown'}${ctx}`
              })
              const title = filter
                ? `Models matching "${filter}" (${matches.length}/${list.length}):`
                : `Available models (${list.length}):`
              if (addSystemMessageRef.current) {
                addSystemMessageRef.current([title, ...lines].join('\n'))
                return
              }
              // Without a transcript to write into, the list rides on the toast:
              // capped (a whole catalog can run to hundreds of rows), one model
              // per line, and up long enough to read.
              const shown = lines.slice(0, TOAST_MODEL_LINES)
              const more = lines.length - shown.length
              if (more > 0) shown.push(t('chat.slashModelsMore', { count: more }))
              toast.info(title, {
                description: shown.join('\n'),
                duration: TOAST_MODEL_MS,
                style: { whiteSpace: 'pre-line' },
              })
            })
            .catch((err: unknown) =>
              toast.error(
                t('chat.slashModelListFailed', {
                  message: err instanceof Error ? err.message : String(err),
                }),
              ),
            )
          return
        }
        // pin the router to a tier (/c0-/c3), or to a
        // directly-named model (/use <model-id>). Both ride the same RPC; the
        // command word says which parameter to send.
        case 'router.hold.set': {
          if ((commandName || '').toLowerCase() === '/use') {
            const model = args.trim()
            if (!model) {
              toast.warning(t('chat.slashUseNeedsModel'))
              return
            }
            rpc
              .call('router.hold.set', { key, model })
              .then(() => {
                onRouteHoldChangeRef.current?.()
                toast.info(t('chat.routePinned', { target: model }))
              })
              .catch((err: unknown) =>
                toast.error(
                  t('chat.slashRouterPinFailed', {
                    message: err instanceof Error ? err.message : String(err),
                  }),
                ),
              )
            return
          }
          const tier = (commandName || '').replace(/^\//, '').toLowerCase()
          rpc
            .call('router.hold.set', { key, tier })
            .then((res: unknown) => {
              onRouteHoldChangeRef.current?.()
              const model = (res as { model?: string })?.model
              toast.info(
                t('chat.slashRouterPinned', { target: tier + (model ? ' → ' + model : '') }),
              )
            })
            .catch((err: unknown) =>
              toast.error(
                t('chat.slashRouterPinFailed', {
                  message: err instanceof Error ? err.message : String(err),
                }),
              ),
            )
          return
        }
        // restore automatic routing.
        case 'router.hold.clear': {
          rpc
            .call('router.hold.clear', { key })
            .then((res: unknown) => {
              onRouteHoldChangeRef.current?.()
              toast.info(
                (res as { cleared?: boolean })?.cleared
                  ? t('chat.slashRoutingRestored')
                  : t('chat.slashRoutingAlready'),
              )
            })
            .catch((err: unknown) =>
              toast.error(
                t('chat.slashRouterUnpinFailed', {
                  message: err instanceof Error ? err.message : String(err),
                }),
              ),
            )
          return
        }
        // `/restart [force]` — the gateway restarts itself in place. The socket
        // drops (close 1012) and the client reconnects on its own; the "back"
        // toast fires on the first reconnect after that drop.
        case 'gateway.restart':
        case '/restart': {
          const mode = args.trim().toLowerCase() === 'force' ? 'force' : 'safe'
          rpc
            .call('gateway.restart', { key, mode, reason: 'slash_command' })
            .then((res: unknown) => {
              const payload = res as { accepted?: boolean; alreadyPending?: boolean } | null
              if (payload?.alreadyPending) {
                toast.info(t('chat.slashRestartPending'))
                return
              }
              toast.info(
                t(mode === 'force' ? 'chat.slashRestartForced' : 'chat.slashRestartRequested'),
              )
              watchGatewayReconnect(rpc, () => toast.success(t('chat.slashRestartDone')))
            })
            .catch((err: unknown) =>
              toast.error(
                t('chat.slashRestartFailed', {
                  message: err instanceof Error ? err.message : String(err),
                }),
              ),
            )
          return
        }
        default:
          // An unmapped action (chat.js: switch falls through with no-op).
          return
      }
    },
    [rpc, queryClient],
  )

  // `_executeSlashCommand`: lazy-load the catalog if it
  // hasn't resolved yet (`if (!_slashCatalogLoaded) await
  // _loadSlashCommands;`), THEN split cmd + args, look up the map, toast on
  // an unknown command, else dispatch. This covers the narrow race where a
  // user submits e.g. `/reset⏎` before the mount effect's catalog load has
  // resolved — legacy still executes it; without this await the map would be
  // empty and every command would toast "Unsupported command".
  // Returns a Promise<boolean> (async, unlike legacy's fire-and-await-able
  // async function) — callers that don't need to block on it can ignore the
  // promise, mirroring how legacy callers rarely await `_executeSlashCommand`.
  const execute = useCallback(
    async (text: string): Promise<boolean> => {
      if (!catalogLoadedRef.current) await loadCatalog()
      const parts = text.trim().split(/\s+/)
      const cmdText = parts[0] ?? ''
      const rest = parts.slice(1)
      const cmd = commandMapRef.current.get(slashCommandKey(cmdText))
      if (!cmd) {
        toast.warning(t('chat.slashUnsupported', { command: cmdText }))
        return true
      }
      dispatch(cmd, rest.join(' '))
      return true
    },
    [dispatch, loadCatalog],
  )

  const isCommand = useCallback(
    async (text: string): Promise<boolean> => {
      if (!catalogLoadedRef.current) await loadCatalog()
      return commandMapRef.current.has(slashCommandKey(text))
    },
    [loadCatalog],
  )

  return { commands, execute, isCommand }
}
