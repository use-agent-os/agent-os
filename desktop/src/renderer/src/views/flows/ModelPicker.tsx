import { Check, Search } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useStore } from 'zustand'
import { t } from '~/i18n'
import { cn } from '~/lib/utils'
import { useEditorEnv } from './editor-context'
import { Floating, moveActive, type Box } from './Floating'
import { modelPriceHint } from './price'
import {
  groupModels,
  modelBlockReason,
  modelCaps,
  modelInput,
  providerName,
  resolvedComboPatch,
  staleModelValues,
} from './schema-logic'
import type { ModelWire } from './types'

interface Row {
  id: string
  model: ModelWire | null
  blocked: string | null
}

/**
 * The model widget's popover: Auto first, then the registry grouped by
 * provider. A model without a key, or one that cannot take what is wired
 * into the node (images on a model that cannot edit, more images than it
 * takes), is greyed with the reason instead of its price.
 */
export function ModelPicker({
  nodeId,
  anchor,
  bounds,
  onClose,
}: {
  nodeId: string
  anchor: Box
  /** The canvas: the picker stays inside it. */
  bounds?: Box | null
  onClose: () => void
}) {
  const env = useEditorEnv()
  const node = useStore(env.store, (s) => s.nodes.find((n) => n.id === nodeId))
  const imageCount = useStore(env.store, (s) => {
    const type = s.nodes.find((n) => n.id === nodeId)?.data.type ?? ''
    const multi = env.schemas.get(type)?.inputs.find((i) => i.multi && i.type === 'IMAGE')
    if (!multi) return 0
    return s.edges.filter((e) => e.target === nodeId && e.targetHandle === multi.name).length
  })
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)

  const schema = node ? env.schemas.get(node.data.type) : undefined
  const spec = modelInput(schema)
  const models = env.models
  const current = spec ? String(node?.data.values[spec.name] ?? spec.default ?? 'auto') : 'auto'
  const defaultModel = models?.models.find((m) => m.id === models.default) ?? null

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase()
    const list = (models?.models ?? []).filter(
      (m) =>
        !q ||
        m.display.toLowerCase().includes(q) ||
        m.id.toLowerCase().includes(q) ||
        m.provider.toLowerCase().includes(q),
    )
    return groupModels(list, current)
  }, [models, query, current])

  const rows: Row[] = useMemo(() => {
    const out: Row[] = []
    if (!query.trim()) {
      out.push({
        id: 'auto',
        model: null,
        blocked: defaultModel
          ? modelBlockReason(defaultModel, imageCount)
          : t('flows.model.autoNone'),
      })
    }
    for (const g of groups) {
      for (const m of g.models)
        out.push({ id: m.id, model: m, blocked: modelBlockReason(m, imageCount) })
    }
    return out
  }, [groups, query, defaultModel, imageCount])

  const indexOf = new Map(rows.map((row, i) => [row.id, i]))

  if (!node || !schema || !spec) return null

  const choose = (row: Row) => {
    if (row.blocked) return
    const next = row.model ?? defaultModel
    // Drop the choices the new model does not offer, then write what its
    // combos will show (e.g. FLUX's first size), so the save matches the screen.
    const stale = staleModelValues(schema, node.data.values, next)
    const kept: Record<string, unknown> = { ...node.data.values, [spec.name]: row.id }
    for (const key of stale) delete kept[key]
    const patch = { [spec.name]: row.id, ...resolvedComboPatch(schema, kept, next) }
    env.store.getState().patchValues(
      nodeId,
      patch,
      stale.filter((k) => !(k in patch)),
    )
    onClose()
  }

  const renderRow = (row: Row) => {
    const i = indexOf.get(row.id) ?? -1
    const isActive = i === active
    const selected = row.id === current
    const label = row.model ? row.model.display : t('flows.model.auto')
    // A model that cannot take the job says why where its caps would be.
    const detail =
      row.blocked ??
      (row.model
        ? modelCaps(row.model)
        : defaultModel
          ? defaultModel.display
          : t('flows.model.autoNone'))
    const aside = row.blocked ? null : row.model ? modelPriceHint(row.model) : null
    return (
      <button
        key={row.id}
        type="button"
        role="option"
        aria-selected={selected}
        aria-disabled={Boolean(row.blocked) || undefined}
        className={cn('flw-pick', isActive && 'is-active', row.blocked && 'is-off')}
        title={row.blocked ?? undefined}
        onMouseEnter={() => setActive(i)}
        onClick={() => choose(row)}
      >
        <span className="flw-pick__check" aria-hidden>
          {selected ? <Check className="size-3" strokeWidth={2.5} /> : null}
        </span>
        <span className="flw-pick__text">
          <span className="flw-pick__name">
            {label}
            {row.model && row.model.id === models?.default ? (
              <span className="flw-pick__default">{t('flows.model.default')}</span>
            ) : null}
          </span>
          <span className="flw-pick__caps">{detail}</span>
        </span>
        {aside ? <span className="flw-pick__aside">{aside}</span> : null}
      </button>
    )
  }

  return (
    <Floating
      anchor={anchor}
      bounds={bounds}
      onClose={onClose}
      label={t('flows.model.title')}
      className="flw-pop--models"
    >
      <div className="flw-pop__search">
        <Search className="size-3.5" strokeWidth={1.75} aria-hidden />
        <input
          data-autofocus
          value={query}
          placeholder={t('flows.model.title')}
          aria-label={t('flows.model.title')}
          onChange={(e) => {
            setQuery(e.target.value)
            setActive(0)
          }}
          onKeyDown={(e) => {
            const next = moveActive(e.key, active, rows.length)
            if (next !== null) {
              e.preventDefault()
              setActive(next)
            } else if (e.key === 'Enter') {
              e.preventDefault()
              const row = rows[active]
              if (row) choose(row)
            }
          }}
        />
        {imageCount > 0 ? (
          <span className="flw-pop__meta">
            {imageCount === 1
              ? t('flows.model.connected.one')
              : t('flows.model.connected.many').replace('{n}', String(imageCount))}
          </span>
        ) : null}
      </div>
      <div className="flw-pop__list" role="listbox" aria-label={t('flows.model.title')}>
        {!models ? <p className="flw-pop__empty">{t('flows.model.loading')}</p> : null}
        {!query.trim() && rows[0]?.id === 'auto' ? renderRow(rows[0]) : null}
        {groups.map((g) => (
          <div key={g.provider} role="group" aria-label={providerName(g.provider)}>
            <div className="flw-pop__group">
              <span>{providerName(g.provider)}</span>
              <span className="flw-pop__group-meta" data-ok={g.available || undefined}>
                {g.available ? t('flows.model.keyOk') : t('flows.model.noKey')}
              </span>
            </div>
            {g.models.map((m) => renderRow(rows.find((r) => r.id === m.id)!))}
          </div>
        ))}
      </div>
      <div className="flw-pop__foot">{t('flows.model.foot')}</div>
    </Floating>
  )
}
