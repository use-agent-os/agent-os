import { Search } from 'lucide-react'
import { useMemo, useState } from 'react'
import { t, type MessageKey } from '~/i18n'
import { cn } from '~/lib/utils'
import { typeColor } from './editor-context'
import { Floating, moveActive, pointBox, type Box } from './Floating'
import { NodeIcon } from './icons'
import { groupedNodes, searchNodes, type TypeFilter } from './search'
import type { NodeSchema, NodesPayload } from './types'

/**
 * The add-node search: from a double-click, the + button, or a link let go
 * over empty canvas (then only nodes that can take the link's other end are
 * listed, and picking one connects it). Arrow keys move, Return adds.
 */
export function AddNodeMenu({
  at,
  bounds,
  nodes,
  filter,
  onPick,
  onClose,
}: {
  at: { x: number; y: number }
  /** The canvas: the menu stays inside it. */
  bounds?: Box | null
  nodes: NodesPayload
  filter: TypeFilter | null
  onPick: (schema: NodeSchema) => void
  onClose: () => void
}) {
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const anchor = useMemo(() => pointBox(at.x, at.y), [at])

  const sections = useMemo(() => {
    if (query.trim()) return [{ group: null, nodes: searchNodes(nodes.nodes, query, filter) }]
    return groupedNodes(nodes.nodes, filter).map((g) => ({ group: g.group, nodes: g.nodes }))
  }, [nodes, query, filter])
  const flat = sections.flatMap((s) => s.nodes)
  const indexOf = new Map(flat.map((schema, i) => [schema.id, i]))

  const pick = (schema: NodeSchema | undefined) => {
    if (!schema) return
    onPick(schema)
    onClose()
  }

  return (
    <Floating
      anchor={anchor}
      bounds={bounds}
      onClose={onClose}
      label={t('flows.editor.add')}
      className="flw-pop--add"
    >
      <div className="flw-pop__search">
        <Search className="size-3.5" strokeWidth={1.75} aria-hidden />
        <input
          data-autofocus
          value={query}
          placeholder={t('flows.add.placeholder')}
          aria-label={t('flows.add.placeholder')}
          onChange={(e) => {
            setQuery(e.target.value)
            setActive(0)
          }}
          onKeyDown={(e) => {
            const next = moveActive(e.key, active, flat.length)
            if (next !== null) {
              e.preventDefault()
              setActive(next)
            } else if (e.key === 'Enter') {
              e.preventDefault()
              pick(flat[active])
            }
          }}
        />
        {filter ? (
          <span className="flw-pop__meta" style={{ color: typeColor(filter.type, nodes) }}>
            {t(filter.side === 'input' ? 'flows.add.takes' : 'flows.add.gives').replace(
              '{type}',
              filter.type,
            )}
          </span>
        ) : null}
      </div>
      <div className="flw-pop__list" role="listbox" aria-label={t('flows.editor.add')}>
        {flat.length === 0 ? <p className="flw-pop__empty">{t('flows.add.empty')}</p> : null}
        {sections.map((section) => (
          <div key={section.group ?? 'results'} role="group">
            {section.group ? (
              <div className="flw-pop__group">
                <span>{t(`flows.group.${section.group}` as MessageKey)}</span>
              </div>
            ) : null}
            {section.nodes.map((schema) => {
              const i = indexOf.get(schema.id) ?? -1
              return (
                <button
                  key={schema.id}
                  type="button"
                  role="option"
                  aria-selected={i === active}
                  className={cn('flw-pick', i === active && 'is-active')}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => pick(schema)}
                >
                  <span className="flw-pick__icon" aria-hidden>
                    <NodeIcon
                      id={schema.id}
                      group={schema.group}
                      className="size-3.5"
                      strokeWidth={1.75}
                    />
                  </span>
                  <span className="flw-pick__text">
                    <span className="flw-pick__name">{schema.display}</span>
                    {schema.description ? (
                      <span className="flw-pick__caps">{schema.description}</span>
                    ) : null}
                  </span>
                  {schema.is_api ? (
                    <span className="flw-pick__aside">{t('flows.add.api')}</span>
                  ) : null}
                </button>
              )
            })}
          </div>
        ))}
      </div>
    </Floating>
  )
}
