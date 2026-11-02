// Chat transcript — Image Flow change cards.
//
// `flow_create` / `flow_update` publish an
// `application/vnd.agentos.flow-change+json` artifact (docs/flows.md, "Agents
// can build flows"): one revision of one flow, as the agent saved it. It shares
// the flow-run card's placeholder, lazy chunk and mounter (flowRun.ts), and
// its `flowrun-card` classes, so it costs the chat page nothing and both
// skins draw it without rules of their own; `data-flowrun-kind="change"` and
// `data-flowrun-status` (created | updated) tell them apart.
//
// A snapshot by design: it says what that save did. "Open in Flows" (desktop
// only, via the transcript's `onOpenFlow`) shows the flow as it is now.
//
// SECURITY: payload strings reach the DOM through `textContent` only.

import { t } from '@/i18n'
import '@/i18n/en/flowcards'

import { FLOW_CHANGE_ARTIFACT_MIME } from './flowRunMime'

export { FLOW_CHANGE_ARTIFACT_MIME }

export type FlowChangeKind = 'created' | 'updated'

export interface FlowPrice {
  kind: 'free' | 'usd' | 'range' | 'unknown'
  usd?: number
  min?: number
  max?: number
  approximate: boolean
}

export interface FlowChangeCard {
  flowId: string
  name: string
  rev: number
  change: FlowChangeKind
  summary: string
  nodeCount: number
  estimate: FlowPrice | null
}

type Obj = Record<string, unknown>

function obj(value: unknown): Obj | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Obj) : null
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function num(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function price(value: unknown): FlowPrice | null {
  const row = obj(value)
  const kind = text(row?.kind)
  if (!row || !['free', 'usd', 'range', 'unknown'].includes(kind)) return null
  return {
    kind: kind as FlowPrice['kind'],
    usd: num(row.usd),
    min: num(row.min),
    max: num(row.max),
    approximate: row.approximate === true,
  }
}

/** The card the artifact describes, or null when it is not a flow change. */
export function normalizeFlowChangePayload(raw: unknown): FlowChangeCard | null {
  const row = obj(raw)
  const flowId = text(row?.flow_id)
  if (!row || row.kind !== 'flow_change' || !flowId) return null
  const rev = num(row.rev)
  const nodes = num(row.node_count)
  return {
    flowId,
    name: text(row.name) || flowId,
    rev: rev !== undefined && rev > 0 ? Math.floor(rev) : 0,
    change: row.change === 'created' ? 'created' : 'updated',
    summary: text(row.summary),
    nodeCount: nodes !== undefined && nodes > 0 ? Math.floor(nodes) : 0,
    estimate: price(row.estimate),
  }
}

function usd(value: number): string {
  if (value === 0 || value >= 0.1) return `$${value.toFixed(2)}`
  return value >= 0.01 ? `$${value.toFixed(3)}` : `$${value.toFixed(4)}`
}

/** `free`, `~$0.067`, `~$0.0034–$0.10`; null when it cannot be told. */
export function flowPriceText(p: FlowPrice | null): string | null {
  if (!p) return null
  const mark = p.approximate ? '~' : ''
  if (p.kind === 'free') return 'free'
  if (p.kind === 'usd' && p.usd !== undefined) return `${mark}${usd(p.usd)}`
  if (p.kind === 'range' && p.min !== undefined && p.max !== undefined) {
    return p.min === p.max ? `${mark}${usd(p.min)}` : `${mark}${usd(p.min)}–${usd(p.max)}`
  }
  return null
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  content?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  node.className = className
  if (content !== undefined) node.textContent = content
  return node
}

/** Build the change card into `host`'s body (replacing what was there). */
export function renderFlowChange(
  host: HTMLElement,
  card: FlowChangeCard,
  options: { openFlow: ((flowId: string) => void) | null },
): HTMLElement {
  const body = host.querySelector<HTMLElement>('.msg-artifact-flowrun__body') ?? host
  const root = el('div', 'flowrun-card flowrun-card--change')
  root.setAttribute('data-flowrun-kind', 'change')
  root.setAttribute('data-flowrun-status', card.change)
  root.setAttribute('data-flowrun-flow-id', card.flowId)

  const head = el('div', 'flowrun-card__head')
  const title = el('span', 'flowrun-card__title', card.name)
  title.title = card.flowId
  head.append(
    el('span', 'flowrun-card__eyebrow', t('flowcards.eyebrow')),
    title,
    el(
      'span',
      'flowrun-card__status',
      card.change === 'created' ? t('flowcards.changeCreated') : t('flowcards.changeUpdated'),
    ),
  )
  root.append(head)

  if (card.summary) root.append(el('p', 'flowrun-card__summary', card.summary))

  const meta = el('div', 'flowrun-card__meta')
  const facts: string[] = []
  if (card.nodeCount > 0) facts.push(t('flowcards.changeNodes', { count: String(card.nodeCount) }))
  const cost = flowPriceText(card.estimate)
  if (cost === 'free') facts.push(t('flowcards.changeFree'))
  else if (cost) facts.push(t('flowcards.changePerRun', { cost }))
  else facts.push(t('flowcards.changeCostUnknown'))
  if (card.rev > 0) facts.push(t('flowcards.changeRev', { rev: String(card.rev) }))
  facts.forEach((fact) => meta.append(el('span', 'flowrun-card__fact', fact)))
  root.append(meta)

  if (options.openFlow) {
    const foot = el('div', 'flowrun-card__foot')
    const open = el('button', 'flowrun-card__open', t('flowcards.open'))
    open.type = 'button'
    open.setAttribute('data-flowrun-action', 'open')
    open.setAttribute('data-flowrun-flow', card.flowId)
    foot.append(open)
    root.append(foot)
  }

  body.replaceChildren(root)
  return root
}
