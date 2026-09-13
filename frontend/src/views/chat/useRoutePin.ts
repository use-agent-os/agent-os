import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  routerFxNormalizeTier,
  type RouterFxDecision,
  type RouterFxTierConfig,
} from './transcript/routerFx'
import type { WsRpcClient } from '@/lib/ws-rpc'
import { t } from '@/i18n'
import '@/i18n/en/chat'

/**
 * State for the composer's route picker: which tier the user has pinned, which
 * tiers can be pinned, and what the router actually did last turn.
 *
 * The pin itself lives in the gateway's `RouterControlHoldStore` — process
 * memory the browser cannot see — so this hook reads it back over
 * `router.hold.get` rather than mirroring it locally. That read is what makes a
 * reload show the real pin instead of a hopeful guess, and it is why a pin set
 * from a slash command (`/c3`) and one set from the picker converge on the same
 * label. `router.hold.get` deliberately does not error when the router is off,
 * so mounting the picker on a gateway with no Pilot Router is silent.
 */

/** One pinnable route, as reported by `router.hold.get`. */
export interface RoutePinTier {
  tier: string
  model: string
}

/** One directly-pinnable model from the active provider's catalog. */
export interface RoutePinModel {
  id: string
  name: string
}

export interface RoutePinState {
  /** Whether the Pilot Router is configured and on. Drives the disabled state. */
  enabled: boolean
  /** Pinnable text tiers, config order. Empty while loading or when disabled. */
  tiers: RoutePinTier[]
  /**
   * The tiers an image turn can land on — shown, never pinned. The router picks
   * the vision route before holds are consulted, so a pin here could not take
   * effect; they are kept apart from `tiers` so no caller can offer them as a
   * choice. Several can be listed: the image branch picks at random among every
   * vision-capable tier.
   */
  imageTiers: RoutePinTier[]
  /**
   * Every model of the ACTIVE provider. Routing runs through one provider, so a
   * model from any other provider in the catalog would be sent to this one
   * under a name it does not know — those are filtered out server-side rather
   * than offered and rejected.
   */
  models: RoutePinModel[]
  /** The pinned tier, or null when routing is automatic or a model is pinned. */
  pinned: string | null
  /** The directly-pinned model id, or null when a tier (or nothing) is pinned. */
  pinnedModel: string | null
  /**
   * Whether the route is pinned at all, either way. Derived here rather than
   * recomputed per consumer: a caller that checks only `pinned` silently treats
   * a model pin as automatic routing, which is how the router-fx strip once
   * kept animating over a pinned model.
   */
  isPinned: boolean
  /** The tier the router last actually used, pin or not. Labels the Auto state. */
  lastRoutedTier: string | null
  /**
   * The model that tier resolved to on that turn, straight from the decision.
   * The tier alone cannot be resolved to a model client-side for every route:
   * `router.hold.get` reports pinnable TEXT tiers only, so an image turn names
   * a tier the picker has no row for. The decision carries the model that ran,
   * which is also the only honest source when several vision tiers are
   * configured — the router picks among them per turn.
   */
  lastRoutedModel: string | null
  /**
   * Set when the last turn was routed to a vision tier while a pin was active.
   * Image turns are chosen before holds are consulted in the router step, so a
   * pinned text tier genuinely does not run them — the picker says so rather
   * than claiming a route the turn did not take. It stays false while routing
   * is automatic: there is no pin for an image turn to override, and saying
   * otherwise names a selection the user never made.
   */
  imageOverride: boolean
  /** True while a pin/clear round-trip is in flight. */
  busy: boolean
}

export interface RoutePinApi extends RoutePinState {
  pin: (tier: string) => void
  pinModel: (model: string) => void
  clear: () => void
}

interface HoldGetResult {
  enabled?: boolean
  provider?: string
  hold?: { tier?: string; model?: string; targetType?: string } | null
  tiers?: { tier?: string; model?: string }[]
  imageTiers?: { tier?: string; model?: string }[]
}

const EMPTY_TIERS: RoutePinTier[] = []
const EMPTY_MODELS: RoutePinModel[] = []

interface HoldSlice {
  session: string
  enabled: boolean
  provider: string
  tiers: RoutePinTier[]
  imageTiers: RoutePinTier[]
  pinned: string | null
  pinnedModel: string | null
}

interface RoutedSlice {
  session: string
  lastRoutedTier: string | null
  lastRoutedModel: string | null
  /**
   * Whether the last turn ran on a vision tier. Whether that OVERRODE anything
   * depends on the hold, which lives in the other slice — the two are combined
   * at the return rather than here, so a decision that arrives before the hold
   * read lands is not permanently mislabelled.
   */
  imageRoute: boolean
}

const EMPTY_HOLD = {
  enabled: false,
  provider: '',
  tiers: EMPTY_TIERS,
  imageTiers: EMPTY_TIERS,
  pinned: null,
  pinnedModel: null,
} as const
const EMPTY_ROUTED = { lastRoutedTier: null, lastRoutedModel: null, imageRoute: false } as const

export function useRoutePin(
  rpc: WsRpcClient,
  sessionKey: string,
  /**
   * Tier config from `config.get`, used only as a fallback model label while the
   * first `router.hold.get` is in flight so the button does not flash empty.
   */
  tierConfigs?: Record<string, RouterFxTierConfig>,
): RoutePinApi {
  // Both slices are STAMPED with the session they describe rather than reset on
  // switch. Holds are per-session, so the previous session's pin must not label
  // the new one — but clearing state from an effect costs an extra render pass
  // and a cascading-render lint waiver. Stamping lets the reads below simply
  // ignore anything that does not belong to the current session, which also
  // discards a slow response that lands after the user has moved on.
  const [hold, setHold] = useState<HoldSlice>(() => ({ session: '', ...EMPTY_HOLD }))
  const [routed, setRouted] = useState<RoutedSlice>(() => ({ session: '', ...EMPTY_ROUTED }))
  const [busy, setBusy] = useState(false)
  // Whether a read has gone out over a live socket yet. The mount read waits
  // for the first connection itself; the reconnect listener below must not
  // double it, only cover the connections after that one.
  const connectedOnce = useRef(false)

  const live = hold.session === sessionKey ? hold : EMPTY_HOLD
  const liveRouted = routed.session === sessionKey ? routed : EMPTY_ROUTED

  const refresh = useCallback(() => {
    const forSession = sessionKey
    // Wait for the socket. The chat mounts before the gateway connection is
    // up (the desktop app opens straight onto the home chat while the gateway
    // is still starting), and a call on a closed socket rejects at once with
    // "Not connected" — which the catch below would file as "router off" and
    // leave the picker disabled until the next session switch.
    rpc
      .waitForConnection()
      .then(() => {
        connectedOnce.current = true
        return rpc.call('router.hold.get', { key: forSession })
      })
      .then((res: unknown) => {
        const result = (res ?? {}) as HoldGetResult
        // A model pin still names the tier hosting it; only `targetType` says
        // which of the two the user actually chose, so the picker must not read
        // the tier as a tier selection.
        const byModel = result.hold?.targetType === 'model'
        const readTiers = (rows: { tier?: string; model?: string }[] | undefined) =>
          (Array.isArray(rows) ? rows : [])
            .map((row) => ({
              tier: routerFxNormalizeTier(row?.tier || ''),
              model: typeof row?.model === 'string' ? row.model : '',
            }))
            .filter((row) => row.tier)
        setHold({
          session: forSession,
          enabled: result.enabled === true,
          provider: typeof result.provider === 'string' ? result.provider : '',
          pinned: byModel ? null : routerFxNormalizeTier(result.hold?.tier || '') || null,
          pinnedModel: byModel ? String(result.hold?.model || '') || null : null,
          tiers: readTiers(result.tiers),
          imageTiers: readTiers(result.imageTiers),
        })
      })
      .catch(() => {
        // A gateway without the RPC (older build) or a dropped socket: leave the
        // picker disabled rather than surfacing an error the user cannot act on.
        setHold({ session: forSession, ...EMPTY_HOLD })
      })
  }, [rpc, sessionKey])

  useEffect(() => {
    refresh()
  }, [refresh])

  // Re-read after every reconnect. The hold store is gateway process memory:
  // a restart drops every pin, so a label carried over the gap would claim a
  // route that is no longer in force. `_state` is the client's own connection
  // signal. The very first connection is the mount read's to handle (it is
  // waiting on it), so only later ones trigger a read here.
  useEffect(
    () =>
      rpc.on('_state', (state: unknown) => {
        if (state === 'connected' && connectedOnce.current) refresh()
      }),
    [rpc, refresh],
  )

  // The catalog is global, not per-session, and only worth fetching once the
  // active provider is known — it is the filter that makes the list pinnable.
  // Read from the raw slice, NOT the session-scoped `live`: a session switch
  // blanks `live` until the new read lands, which would drop the provider and
  // refetch the whole catalog for a fact that did not change.
  const [models, setModels] = useState<RoutePinModel[]>(EMPTY_MODELS)
  const provider = hold.provider
  useEffect(() => {
    if (!provider) return
    let ignore = false
    rpc
      .call('models.list', { provider })
      .then((res: unknown) => {
        if (ignore) return
        const rows = Array.isArray(res) ? (res as Record<string, unknown>[]) : []
        setModels(
          rows
            .map((row) => ({
              id: String(row?.id ?? ''),
              name: String(row?.name || row?.id || ''),
            }))
            .filter((row) => row.id),
        )
      })
      .catch(() => {
        // No catalog is a usable state: the tier rows still pin.
        if (!ignore) setModels(EMPTY_MODELS)
      })
    return () => {
      ignore = true
    }
  }, [rpc, provider])

  // Track what the router actually did, which is the only honest source for the
  // Auto label and for noticing that an image turn bypassed the pin. Re-subscribed
  // per session so the stamp is captured without a render-time ref write.
  useEffect(() => {
    const forSession = sessionKey
    return rpc.on('session.event.router_decision', (payload: unknown) => {
      const decision = (payload ?? {}) as RouterFxDecision
      const tier = routerFxNormalizeTier(String(decision.tier || decision.routed_tier || ''))
      if (!tier) return
      const source = String(decision.source || decision.routing_source || '').toLowerCase()
      const model = String(decision.model || decision.routed_model || '').trim()
      setRouted({
        session: forSession,
        lastRoutedTier: tier,
        lastRoutedModel: model || null,
        imageRoute: source === 'image_route' || tier === 'image_model',
      })
    })
  }, [rpc, sessionKey])

  const pin = useCallback(
    (tier: string) => {
      const target = routerFxNormalizeTier(tier)
      if (!target) return
      const forSession = sessionKey
      setBusy(true)
      rpc
        .call('router.hold.set', { key: sessionKey, tier: target })
        .then((res: unknown) => {
          const model = (res as { model?: string })?.model
          setHold((prev) =>
            prev.session === forSession ? { ...prev, pinned: target, pinnedModel: null } : prev,
          )
          toast.info(t('chat.routePinned', { target: target + (model ? ' → ' + model : '') }))
        })
        .catch((err: unknown) =>
          toast.error(
            t('chat.slashRouterPinFailed', {
              message: err instanceof Error ? err.message : String(err),
            }),
          ),
        )
        .finally(() => setBusy(false))
    },
    [rpc, sessionKey],
  )

  const pinModel = useCallback(
    (model: string) => {
      const target = model.trim()
      if (!target) return
      const forSession = sessionKey
      setBusy(true)
      rpc
        .call('router.hold.set', { key: forSession, model: target })
        .then(() => {
          setHold((prev) =>
            prev.session === forSession ? { ...prev, pinned: null, pinnedModel: target } : prev,
          )
          toast.info(t('chat.routePinned', { target }))
        })
        .catch((err: unknown) =>
          // `router.unknown_model` names the active provider — pass the message
          // through rather than restating it less precisely.
          toast.error(
            t('chat.slashRouterPinFailed', {
              message: err instanceof Error ? err.message : String(err),
            }),
          ),
        )
        .finally(() => setBusy(false))
    },
    [rpc, sessionKey],
  )

  const clear = useCallback(() => {
    const forSession = sessionKey
    setBusy(true)
    rpc
      .call('router.hold.clear', { key: forSession })
      .then(() => {
        setHold((prev) =>
          prev.session === forSession ? { ...prev, pinned: null, pinnedModel: null } : prev,
        )
        toast.info(t('chat.slashRoutingRestored'))
      })
      .catch((err: unknown) =>
        toast.error(
          t('chat.slashRouterUnpinFailed', {
            message: err instanceof Error ? err.message : String(err),
          }),
        ),
      )
      .finally(() => setBusy(false))
  }, [rpc, sessionKey])

  // Fall back to the config-derived tier list until the first read lands, so the
  // menu is populated on the very first open rather than after a round-trip.
  const effectiveTiers = useMemo(() => {
    if (live.tiers.length > 0) return live.tiers
    if (!tierConfigs) return EMPTY_TIERS
    return Object.entries(tierConfigs)
      .filter(([, cfg]) => !cfg.imageOnly)
      .map(([tier, cfg]) => ({ tier, model: cfg.model || '' }))
  }, [live.tiers, tierConfigs])

  // Same fallback for the image rows, split on the flag the ROUTER splits on:
  // `supports_image`, not `image_only`. A text tier that also takes images is a
  // candidate for an image turn, and pinnability is a separate question the
  // list above already answers.
  const effectiveImageTiers = useMemo(() => {
    if (live.imageTiers.length > 0) return live.imageTiers
    if (!tierConfigs) return EMPTY_TIERS
    return Object.entries(tierConfigs)
      .filter(([, cfg]) => cfg.supportsImage || cfg.imageOnly)
      .map(([tier, cfg]) => ({ tier, model: cfg.model || '' }))
  }, [live.imageTiers, tierConfigs])

  const isPinned = live.pinned !== null || live.pinnedModel !== null

  return {
    enabled: live.enabled,
    tiers: effectiveTiers,
    imageTiers: effectiveImageTiers,
    models,
    pinned: live.pinned,
    pinnedModel: live.pinnedModel,
    isPinned,
    lastRoutedTier: liveRouted.lastRoutedTier,
    lastRoutedModel: liveRouted.lastRoutedModel,
    imageOverride: liveRouted.imageRoute && isPinned,
    busy,
    pin,
    pinModel,
    clear,
  }
}
