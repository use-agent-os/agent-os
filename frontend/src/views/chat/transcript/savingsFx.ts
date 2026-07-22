// Chat transcript — SavingsFX streak accounting + optional celebration.
//
// Imperative near-verbatim port of
// `static/js/components/savings-fx.js` plus chat.js:537-566/5184-5248. The
// production burst remains deliberately disabled (the legacy product decision);
// streak, combo and turn-footer state still run for every completed turn.

export const SAVINGS_FX_PREF_KEY = 'agentos.savingsFx'
export const SAVINGS_POPUP_COOLDOWN_MS = 10 * 60 * 1000
export const SAVINGS_POPUP_BURST_ENABLED = false

export interface SavingsUsage extends Record<string, unknown> {
  model?: string
  routed_model?: string
  routed_tier?: string
  routing_source?: string
  routing_confidence?: number
  total_savings_usd?: number
  savings_usd?: number
  total_savings_pct?: number
  savings_pct?: number
  cached_tokens?: number
  cache_hit_active?: boolean
  __savings_ui_suppressed?: boolean
}

export interface SavingsStreak {
  current: number
  max: number
}

interface SavingsFxDeps {
  storage?: Pick<Storage, 'getItem' | 'setItem'> | null
  now?: () => number
  thread?: () => HTMLElement | null
  burstEnabled?: boolean
}

interface Particle {
  x: number
  y: number
  vx: number
  vy: number
  gravity: number
  size: number
  life: number
  decay: number
  color: string
  isStar: boolean
}

function finiteNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

/** savings-fx.js:62-65 — the per-turn, never-cumulative label. */
export function savingsLabel(savePct: unknown): string {
  const pct = finiteNumber(savePct)
  if (!pct || pct < 1) return 'Cost optimized'
  return `Saved ~${Math.round(pct)}%`
}

/** savings-fx.js:100-103 / chat.js:1007-1010. */
export function savingsTurnIdentity(usage: SavingsUsage | null | undefined): string {
  const model = String(usage?.routed_model || usage?.model || '')
  return model ? `${model}|${String(usage?.routed_tier || '')}` : ''
}

/** savings-fx.js:105-111 — c1/c2 (or t1/t2) and named non-flagship tiers qualify. */
export function isSavingsComboTier(tier: unknown): boolean {
  const value = String(tier || '')
    .trim()
    .toLowerCase()
  const numeric = /^c(\d+)$/.exec(value) || /^t(\d+)$/.exec(value)
  if (numeric) return Number(numeric[1]) < 3
  return value !== 'highest' && value !== 'top' && value !== 'flagship'
}

/** savings-fx.js:43-60 — particle intensity, clamped to the legacy 0.25..1 range. */
export function savingsScore(usage: SavingsUsage | null | undefined): number {
  const u = usage || {}
  const savingsUsd =
    typeof u.total_savings_usd === 'number' ? u.total_savings_usd : finiteNumber(u.savings_usd)
  const rawPct =
    typeof u.total_savings_pct === 'number' && u.total_savings_pct > 0
      ? u.total_savings_pct
      : finiteNumber(u.savings_pct)
  const savingsPct = rawPct / 100
  const usdComponent = Math.min(1, savingsUsd / 0.05)
  const confidence = typeof u.routing_confidence === 'number' ? u.routing_confidence : 0.5
  const blended = usdComponent * 0.55 + savingsPct * 0.35 + confidence * 0.1
  return Math.max(0.25, Math.min(1, blended))
}

export function isRoutedSavingsTurn(usage: SavingsUsage | null | undefined): boolean {
  return Boolean(
    usage?.routed_tier &&
    usage.routing_source &&
    usage.routing_source !== 'none' &&
    typeof usage.total_savings_pct === 'number' &&
    usage.total_savings_pct > 0,
  )
}

function defaultStorage(): Pick<Storage, 'getItem' | 'setItem'> | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null
  } catch {
    return null
  }
}

/**
 * Stateful SavingsFX engine. Streak accounting is intentionally independent
 * from the visual preference; the preference only gates `fire()`.
 */
export function createSavingsFx(deps: SavingsFxDeps = {}) {
  const storage = deps.storage === undefined ? defaultStorage() : deps.storage
  const now = deps.now ?? (() => Date.now())
  const thread = deps.thread ?? (() => null)
  const burstEnabled = deps.burstEnabled ?? SAVINGS_POPUP_BURST_ENABLED

  let enabled = true
  try {
    enabled = storage?.getItem(SAVINGS_FX_PREF_KEY) !== '0'
  } catch {
    enabled = true
  }

  let streak = 0
  let maxStreak = 0
  let streakIdentity = ''
  let popupLastTs = 0
  let lastPopupIdentity = ''
  const popupTsByIdentity = new Map<string, number>()
  const activeCanvases = new Set<HTMLCanvasElement>()
  const labels = new Set<HTMLElement>()

  function isEnabled(): boolean {
    return enabled
  }

  function setEnabled(on: boolean): void {
    enabled = !!on
    try {
      storage?.setItem(SAVINGS_FX_PREF_KEY, enabled ? '1' : '0')
    } catch {
      // Client preference persistence is optional; the live instance still updates.
    }
  }

  function resetStreak(): void {
    streak = 0
    streakIdentity = ''
  }

  function getStreak(): SavingsStreak {
    return { current: streak, max: maxStreak }
  }

  function noteTurn(usage: SavingsUsage | null | undefined): void {
    const identity = savingsTurnIdentity(usage)
    if (isRoutedSavingsTurn(usage) && identity && isSavingsComboTier(usage?.routed_tier)) {
      streak = streakIdentity === identity ? streak + 1 : 1
      streakIdentity = identity
      if (streak > maxStreak) maxStreak = streak
    } else if (streak !== 0 || streakIdentity) {
      streak = 0
      streakIdentity = ''
    }
  }

  function viewport(): { width: number; height: number; dpr: number } {
    if (typeof window === 'undefined') return { width: 1024, height: 768, dpr: 1 }
    return {
      width: window.innerWidth,
      height: window.innerHeight,
      dpr: Math.max(1, Math.min(2, window.devicePixelRatio || 1)),
    }
  }

  function deviceMult(): number {
    const width = viewport().width
    if (width < 480) return 0.55
    if (width < 1024) return 0.78
    return 1
  }

  function speedScale(): number {
    const vp = viewport()
    return Math.min(vp.width, vp.height) / 280
  }

  function reducedMotion(): boolean {
    try {
      return window.matchMedia('(prefers-reduced-motion: reduce)').matches
    } catch {
      return false
    }
  }

  function canVibrate(): boolean {
    if (typeof navigator === 'undefined' || typeof navigator.vibrate !== 'function') return false
    const activation = navigator.userActivation
    return !activation || activation.hasBeenActive || activation.isActive
  }

  function showSavingsLabel(usage: SavingsUsage): void {
    if (typeof document === 'undefined' || !document.body) return
    const savePct =
      typeof usage.total_savings_pct === 'number' && usage.total_savings_pct > 0
        ? usage.total_savings_pct
        : 0
    const el = document.createElement('div')
    el.className = 'savings-float'
    if (savePct >= 65) el.classList.add('savings-float--peak')
    el.setAttribute('aria-hidden', 'true')

    const main = document.createElement('span')
    main.className = 'savings-float__main'
    main.textContent = savingsLabel(savePct)
    const sub = document.createElement('span')
    sub.className = 'savings-float__sub'
    sub.textContent = 'this turn'
    el.append(main, sub)
    document.body.appendChild(el)
    labels.add(el)
    window.setTimeout(() => {
      el.remove()
      labels.delete(el)
    }, 2600)
  }

  function pulseBorder(bubble: HTMLElement | null): void {
    if (!bubble?.isConnected) return
    const body = bubble.querySelector<HTMLElement>('.msg-body')
    if (!body) return
    const previousTransition = body.style.transition
    body.style.transition = 'box-shadow 0.25s ease'
    body.style.boxShadow = '0 0 0 2px color-mix(in srgb, var(--warn) 55%, transparent)'
    window.setTimeout(() => {
      body.style.boxShadow = ''
      body.style.transition = previousTransition
    }, 550)
  }

  function star(ctx: CanvasRenderingContext2D, x: number, y: number, radius: number): void {
    const inner = radius * 0.42
    const step = Math.PI / 5
    ctx.beginPath()
    for (let index = 0; index < 10; index += 1) {
      const angle = index * step - Math.PI / 2
      const r = index % 2 === 0 ? radius : inner
      const px = x + Math.cos(angle) * r
      const py = y + Math.sin(angle) * r
      if (index === 0) ctx.moveTo(px, py)
      else ctx.lineTo(px, py)
    }
    ctx.closePath()
    ctx.fill()
  }

  function makeParticles(
    originX: number,
    originY: number,
    count: number,
    confidence: number,
    milestone: boolean,
  ): Particle[] {
    const scale = speedScale()
    return Array.from({ length: count }, (_, index) => {
      const color = milestone
        ? `hsl(${30 + (index / count) * 50},94%,62%)`
        : `hsl(${33 + Math.random() * 22},92%,${55 + Math.random() * 14}%)`
      const isStar = milestone || confidence >= 0.78
      const angle = Math.random() * Math.PI * 2
      const speed = (1.6 + Math.random() * 5.2) * scale * 0.95
      return {
        x: originX,
        y: originY,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        gravity: 0.045 + Math.random() * 0.04,
        size: milestone ? 3.5 + Math.random() * 3.5 : 1.8 + Math.random() * 2.8,
        life: 0.85 + Math.random() * 0.15,
        decay: 0.0035 + Math.random() * 0.008,
        color,
        isStar,
      }
    })
  }

  function spawnCanvas(
    originX: number,
    originY: number,
    count: number,
    duration: number,
    confidence: number,
    milestone: boolean,
  ): void {
    if (typeof document === 'undefined' || !document.body) return
    const canvas = document.createElement('canvas')
    canvas.style.cssText =
      'position:fixed;top:0;left:0;width:100vw;height:100vh;pointer-events:none;z-index:9998;'
    const vp = viewport()
    canvas.width = vp.width * vp.dpr
    canvas.height = vp.height * vp.dpr
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    document.body.appendChild(canvas)
    activeCanvases.add(canvas)
    ctx.scale(vp.dpr, vp.dpr)
    const particles = makeParticles(originX, originY, count, confidence, milestone)
    const startedAt = performance.now()

    const frame = (frameNow: number): void => {
      if ((frameNow - startedAt) / duration >= 1) {
        canvas.remove()
        activeCanvases.delete(canvas)
        return
      }
      ctx.clearRect(0, 0, canvas.width, canvas.height)
      let alive = false
      particles.forEach((particle) => {
        particle.x += particle.vx
        particle.y += particle.vy
        particle.vy += particle.gravity
        particle.vx *= 0.991
        particle.life -= particle.decay
        if (particle.life <= 0) return
        alive = true
        ctx.globalAlpha = particle.life * particle.life
        ctx.fillStyle = particle.color
        if (particle.isStar) star(ctx, particle.x, particle.y, particle.size)
        else {
          ctx.beginPath()
          ctx.arc(particle.x, particle.y, particle.size, 0, 6.2832)
          ctx.fill()
        }
      })
      ctx.globalAlpha = 1
      if (!alive) {
        canvas.remove()
        activeCanvases.delete(canvas)
        return
      }
      requestAnimationFrame(frame)
    }
    requestAnimationFrame(frame)
  }

  function burst(score: number, confidence: number): void {
    const vp = viewport()
    const streakMult = Math.min(2.6, 1 + (streak - 1) * 0.3)
    const count = Math.max(28, Math.min(180, Math.round(score * 90 * deviceMult() * streakMult)))
    const duration = Math.min(3600, 1800 + score * 1500 + Math.hypot(vp.width, vp.height) * 0.18)
    spawnCanvas(vp.width / 2, vp.height * 0.45, count, duration, confidence, false)
  }

  function streakBurst(value: number): void {
    const vp = viewport()
    const count = Math.min(140, Math.round((28 + value * 5) * deviceMult()))
    spawnCanvas(vp.width / 2, vp.height * 0.45, count, 3000, 1, true)
  }

  function fire(bubble: HTMLElement | null, usage: SavingsUsage | null | undefined): void {
    if (!enabled) return
    if (canVibrate()) {
      if (streak >= 5) navigator.vibrate([40, 20, 60, 20, 40])
      else if (streak >= 3) navigator.vibrate([40, 20, 60])
      else navigator.vibrate(30)
    }

    const u = usage || {}
    const score = savingsScore(u)
    const confidence = typeof u.routing_confidence === 'number' ? u.routing_confidence : 0.5
    if (reducedMotion()) {
      pulseBorder(bubble)
      showSavingsLabel(u)
      return
    }
    burst(score, confidence)
    window.setTimeout(() => showSavingsLabel(u), 180)
    if (streak === 3 || streak === 5 || (streak >= 10 && streak % 5 === 0)) {
      window.setTimeout(() => streakBurst(streak), 360)
    }
  }

  /** chat.js:5184-5248 — model-switch suppression, streak update and cooldown. */
  function maybeFire(
    bubble: HTMLElement | null,
    usage: SavingsUsage | null | undefined,
    opts: { animate?: boolean } = {},
  ): boolean {
    const u = usage || {}
    const identity = savingsTurnIdentity(u)
    let suppressPopup = false
    if (identity) {
      const identityChanged = !!(lastPopupIdentity && lastPopupIdentity !== identity)
      lastPopupIdentity = identity
      if (identityChanged) suppressPopup = true
    }
    if (suppressPopup) u.__savings_ui_suppressed = true

    // This ALWAYS runs, including when the product-level burst gate is false.
    noteTurn(u)
    if (!burstEnabled || suppressPopup || opts.animate === false) return false

    const cacheHit = Boolean(u.cache_hit_active || finiteNumber(u.cached_tokens) > 0)
    if (!isRoutedSavingsTurn(u) && !cacheHit) return false
    const timestamp = now()
    const identityLastTs = identity ? popupTsByIdentity.get(identity) || 0 : popupLastTs
    if (!cacheHit && timestamp - identityLastTs < SAVINGS_POPUP_COOLDOWN_MS) return false

    let fxBubble = bubble?.isConnected ? bubble : null
    if (!fxBubble) {
      const assistants = thread()?.querySelectorAll<HTMLElement>('.msg.assistant') || []
      fxBubble = assistants.length ? assistants[assistants.length - 1]! : null
    }
    fire(fxBubble, u)
    popupLastTs = timestamp
    if (identity) popupTsByIdentity.set(identity, timestamp)
    return true
  }

  /** History rerenders recompute the streak oldest→newest without clearing cooldowns. */
  function beginHistoryReplay(): void {
    resetStreak()
    lastPopupIdentity = ''
  }

  function noteHistoryTurn(usage: SavingsUsage | null | undefined): void {
    maybeFire(null, usage, { animate: false })
  }

  function cleanup(): void {
    activeCanvases.forEach((canvas) => canvas.remove())
    activeCanvases.clear()
    labels.forEach((label) => label.remove())
    labels.clear()
  }

  /** chat.js:558-566 — full session-boundary reset. */
  function resetPopupCooldown(): void {
    popupLastTs = 0
    lastPopupIdentity = ''
    popupTsByIdentity.clear()
    resetStreak()
    cleanup()
  }

  return {
    fire,
    maybeFire,
    noteTurn,
    beginHistoryReplay,
    noteHistoryTurn,
    resetStreak,
    resetPopupCooldown,
    cleanup,
    getStreak,
    savingsLabel,
    isEnabled,
    setEnabled,
  }
}

export type SavingsFx = ReturnType<typeof createSavingsFx>
