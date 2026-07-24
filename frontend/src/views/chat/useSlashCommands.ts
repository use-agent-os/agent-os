import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { normalizeSlashCommand, slashCommandKey, type SlashCommand } from './logic'
import { resetSession } from './resetSession'

/**
 * Slash-command catalog + execution (React).
 *
 * Ported from static/js/views/chat.js:2615-2853: the catalog load
 * `_loadSlashCommands` (chat.js:2615, RPC `commands.list_for_surface` with
 * `{ surface: 'web_chat' }`), the command map (chat.js:2621-2627), and the
 * dispatch `_executeSlashCommand` → `_selectSlashCmd` (chat.js:2842/2684).
 *
 * The catalog + the command map live here (one source) so both <SlashMenu> (which
 * renders/filters) and the composer send path (which executes on Enter over a
 * typed `/cmd`) share them — DRY, and avoids two RPC loads.
 *
 * `_selectSlashCmd`'s action switch (chat.js:2691-2839) dispatches on the
 * serialized `execution.action` / `rpc_method`. The RPC-backed branches (reset /
 * usage / model / router.hold.set / router.hold.clear) are ported faithfully as
 * `rpc.call(...) + toast`. The two branches that cross into session/stream
 * ownership delegate through `onSessionAction`: `new_chat` (chat.js:2692-2715)
 * switches/persists/re-subscribes, while `compact_context` (chat.js:2738-2763)
 * uses the composed compaction controller for in-flight UI around the RPC.
 */

export interface UseSlashCommands {
  /** The normalized catalog (chat.js:2620 `_slashCmds`). */
  commands: SlashCommand[]
  /**
   * Execute a typed slash-command line (chat.js:2842 `_executeSlashCommand`).
   * If the catalog hasn't loaded yet, awaits the same load the mount effect
   * kicked off (chat.js:2843 `if (!_slashCatalogLoaded) await
   * _loadSlashCommands();`) before looking the command up — this covers a
   * user submitting a command before the catalog RPC has resolved. Then looks
   * the command up in the map, splits off args, and dispatches. Always
   * resolves true (so the caller does NOT also send the text as a chat
   * message) — including for an unknown command, which legacy toasts a
   * warning for but still swallows rather than sending as text.
   */
  execute: (text: string) => Promise<boolean>
}

export function useSlashCommands(opts?: {
  sessionKey: string
  /** Delegate the session/stream-mutating actions (new_chat / compact) to the
   * transcript owner. Receives the resolved action + the command + args. */
  onSessionAction?: (action: string, cmd: SlashCommand, args: string) => void
  /** Append a system message row (chat.js:2814 `/model` result). */
  addSystemMessage?: (text: string) => void
}): UseSlashCommands {
  const rpc = useRpc()
  const sessionKey = opts?.sessionKey ?? ''
  const [commands, setCommands] = useState<SlashCommand[]>([])
  // chat.js:2628 `_slashCatalogLoaded` — set once the catalog resolves (success
  // or failure), so `execute` (chat.js:2843) knows whether it must await a load
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
  useEffect(() => {
    sessionKeyRef.current = sessionKey
    onSessionActionRef.current = opts?.onSessionAction
    addSystemMessageRef.current = opts?.addSystemMessage
  }, [sessionKey, opts?.onSessionAction, opts?.addSystemMessage])

  // ── Catalog load (chat.js:2615-2635 `_loadSlashCommands`) ─────────────────
  // Extracted so both the mount effect below AND `execute` (chat.js:2843
  // `if (!_slashCatalogLoaded) await _loadSlashCommands();`) can trigger the
  // same load and share one in-flight promise — a command typed before the
  // catalog resolves awaits the load rather than firing a second RPC call.
  // The command map (chat.js:2621-2627): name + every alias → the command. Held
  // in a ref so the imperative `execute` always reads the latest synchronously.
  // `loadCatalog` writes this ref directly (not via a `useEffect` keyed off
  // `commands` state) because `execute` awaits `loadCatalog()` and then reads
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

  // ── _selectSlashCmd action switch (chat.js:2684-2839), RPC-backed branches ──
  const dispatch = useCallback(
    (cmd: SlashCommand, args: string) => {
      const key = sessionKeyRef.current
      const commandName = cmd?.cmd || cmd?.name || ''
      // chat.js:2719 — a `/reset` command whose name is actually `/new` runs the
      // new_chat action. Resolve that remap here rather than recursing.
      let action = cmd?.execution?.action || cmd.cmd || cmd.name || ''
      if (
        (action === 'reset_session' || action === 'sessions.reset' || action === '/reset') &&
        commandName === '/new'
      ) {
        action = 'new_chat'
      }

      switch (action) {
        // chat.js:2692-2715 — delegate the fully wired React session swap.
        case 'new_chat':
        case '/new': {
          if (onSessionActionRef.current) onSessionActionRef.current('new_chat', cmd, args)
          else toast.info('New chat is available from the session menu')
          return
        }
        // chat.js:2716-2737 — reset the current session. (The `/new`-named-but-
        // reset-action remap is resolved above into `new_chat`.)
        case 'reset_session':
        case 'sessions.reset':
        case '/reset': {
          void resetSession(rpc, key)
          return
        }
        // chat.js:2738-2763 — manual compaction. The RPC is fired; the in-thread
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
            .then(() => toast.info('Context compaction requested'))
            .catch((err: unknown) =>
              toast.error(
                'Compaction failed: ' + (err instanceof Error ? err.message : String(err)),
              ),
            )
          return
        }
        // chat.js:2764-2789 — usage status / cost.
        case 'usage_status':
        case 'usage.status':
        case '/usage': {
          const arg = args.trim().toLowerCase()
          if (arg === 'page') {
            toast.info('Usage page is available from the sidebar')
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
                    : 'Usage cost unavailable',
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
              toast.error('Usage failed: ' + (err instanceof Error ? err.message : String(err))),
            )
          return
        }
        // chat.js:2790-2818 — model list (optionally filtered), into a system row.
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
                toast.info(filter ? `No models match "${filter}"` : 'No models available')
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
              const body = [title, ...lines].join('\n')
              if (addSystemMessageRef.current) addSystemMessageRef.current(body)
              else toast.info(title)
            })
            .catch((err: unknown) =>
              toast.error(
                'Model list failed: ' + (err instanceof Error ? err.message : String(err)),
              ),
            )
          return
        }
        // chat.js:2819-2829 — pin the router to a tier (/c0-/c3).
        case 'router.hold.set': {
          const tier = (commandName || '').replace(/^\//, '').toLowerCase()
          rpc
            .call('router.hold.set', { key, tier })
            .then((res: unknown) => {
              const model = (res as { model?: string })?.model
              toast.info('Router pinned to ' + tier + (model ? ' → ' + model : ''))
            })
            .catch((err: unknown) =>
              toast.error(
                'Router pin failed: ' + (err instanceof Error ? err.message : String(err)),
              ),
            )
          return
        }
        // chat.js:2830-2838 — restore automatic routing.
        case 'router.hold.clear': {
          rpc
            .call('router.hold.clear', { key })
            .then((res: unknown) =>
              toast.info(
                (res as { cleared?: boolean })?.cleared
                  ? 'Automatic routing restored'
                  : 'Automatic routing already active',
              ),
            )
            .catch((err: unknown) =>
              toast.error(
                'Router unpin failed: ' + (err instanceof Error ? err.message : String(err)),
              ),
            )
          return
        }
        default:
          // An unmapped action (chat.js: switch falls through with no-op).
          return
      }
    },
    [rpc],
  )

  // chat.js:2842-2853 `_executeSlashCommand`: lazy-load the catalog if it
  // hasn't resolved yet (chat.js:2843 `if (!_slashCatalogLoaded) await
  // _loadSlashCommands();`), THEN split cmd + args, look up the map, toast on
  // an unknown command, else dispatch. This covers the narrow race where a
  // user submits e.g. `/reset⏎` before the mount effect's catalog load has
  // resolved — legacy still executes it; without this await the map would be
  // empty and every command would toast "Unsupported command".
  //
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
        toast.warning('Unsupported command: ' + cmdText)
        return true
      }
      dispatch(cmd, rest.join(' '))
      return true
    },
    [dispatch, loadCatalog],
  )

  return { commands, execute }
}
