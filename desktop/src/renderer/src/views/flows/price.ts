import { t } from '~/i18n'
import type { EstimateResult, ModelWire, Price } from './types'

/**
 * Money as the canvas shows it. Prices from the registry are approximate,
 * and say so with a leading "~" wherever they appear (badge, toolbar, model
 * picker): "~$0.27", "~$0.003–$0.10". A single amount under a cent is
 * "<$0.01", which already says it is not exact, so it never gets a "~" too.
 */

/** Dollars: "$0", "<$0.01", "$0.27", "$12.40", "$140". */
export function usd(n: number): string {
  if (!Number.isFinite(n)) return '$?'
  if (n === 0) return '$0'
  if (n > 0 && n < 0.01) return '<$0.01'
  if (n < 100) return `$${n.toFixed(2)}`
  return `$${Math.round(n)}`
}

/** Dollars inside a range, where "<$0.01" would read oddly: "$0.003". */
function usdBound(n: number): string {
  if (n > 0 && n < 0.01) return `$${Number(n.toPrecision(1))}`
  return usd(n)
}

function amount(n: number, approximate: boolean): string {
  if (n > 0 && n < 0.01) return '<$0.01'
  return `${approximate ? '~' : ''}${usd(n)}`
}

/** The price itself, without the "this run" framing. Null for free / nothing to say. */
export function priceText(p: Price | null | undefined): string | null {
  if (!p) return null
  switch (p.kind) {
    case 'free':
      return null
    case 'usd':
      return p.usd === undefined ? null : amount(p.usd, p.approximate)
    case 'range': {
      if (p.min === undefined || p.max === undefined) return null
      if (p.min === p.max || p.max < 0.01) return amount(p.max, p.approximate)
      return `${p.approximate ? '~' : ''}${usdBound(p.min)}–${usd(p.max)}`
    }
    case 'unknown':
      return '?'
    default:
      return null
  }
}

/** Node badge: "~$0.24", "<$0.01", "~$0.06–$0.24", "?"; null hides it (free, no estimate). */
export function badgeText(p: Price | null | undefined): string | null {
  return priceText(p)
}

export interface EstimateChip {
  /** "This run" */
  lead: string
  /** "~$0.29", "free", "cost unknown" */
  amount: string
  /** "2 cached" or null */
  cached: string | null
  tone: 'free' | 'cost' | 'unknown'
}

/** The toolbar chip: "This run ~$0.29 · 2 cached", "This run · free", "This run · cost unknown". */
export function estimateChip(est: EstimateResult | null | undefined): EstimateChip | null {
  if (!est) return null
  const n = est.cached?.length ?? 0
  const cached = n > 0 ? t('flows.estimate.cached').replace('{n}', String(n)) : null
  const lead = t('flows.estimate.thisRun')
  const total = est.total
  if (total?.kind === 'free') {
    return { lead, amount: t('flows.estimate.free'), cached, tone: 'free' }
  }
  const text = total && total.kind !== 'unknown' ? priceText(total) : null
  if (!text) return { lead, amount: t('flows.estimate.unknown'), cached, tone: 'unknown' }
  return { lead, amount: text, cached, tone: 'cost' }
}

/** One full chip string, for tooltips and tests. */
export function estimateChipText(est: EstimateResult | null | undefined): string {
  const chip = estimateChip(est)
  if (!chip) return ''
  const label = `${chip.lead}${chip.tone === 'cost' ? ' ' : ' · '}${chip.amount}`
  return chip.cached ? `${label} · ${chip.cached}` : label
}

/**
 * The model picker's price hint for one image at the cheapest listed key
 * (contract: per_image[tier] | per_image["<quality>:<size>"] | per_image.default).
 */
export function modelPriceHint(model: ModelWire): string | null {
  const per = model.price?.per_image
  if (!per) return null
  const values = Object.values(per).filter((v) => typeof v === 'number' && Number.isFinite(v))
  if (!values.length) return null
  const cheapest = per.default ?? Math.min(...values)
  return amount(cheapest, model.price?.approximate !== false)
}

export function formatMs(ms: number | undefined): string | null {
  if (ms === undefined || !Number.isFinite(ms)) return null
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const m = Math.floor(ms / 60_000)
  const s = Math.round((ms % 60_000) / 1000)
  return `${m}m ${s}s`
}

/** A running clock: "0:07", "2:05", "1:02:03". */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const sec = String(total % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`
}
