import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { Check } from 'lucide-react'
import {
  catLabel,
  registryKey,
  skillBucket,
  skillDotTitle,
  type RawSkill,
  type RegistryItem,
  type SkillGroup,
} from '@/views/skills/logic'
import { t } from '~/i18n'
import { quick } from '~/lib/motion'
import { skillRowKey } from './logic'
import { RegistryMark, SkillMark } from './SkillMark'

/**
 * The middle column: one row per skill, grouped by provenance for the
 * installed library and flat for a catalog. Rows are buttons, not links —
 * selection is view state, the window stays where it was.
 */
function ListNotes({
  loading,
  error,
  empty,
  emptyText,
}: {
  loading: boolean
  error: string | null
  empty: boolean
  emptyText: string
}) {
  return (
    <>
      {loading ? (
        <p className="sk-list__note" role="status">
          {t('skills.list.loading')}
        </p>
      ) : null}
      {error ? (
        <p className="sk-list__note text-danger">
          {error}
          <br />
          <span className="text-dim">{t('skills.list.retry')}</span>
        </p>
      ) : null}
      {!loading && !error && empty ? <p className="sk-list__note">{emptyText}</p> : null}
    </>
  )
}

export function InstalledList({
  groups,
  loading,
  error,
  emptyText,
  selectedKey,
  onSelect,
}: {
  groups: SkillGroup[]
  loading: boolean
  error: string | null
  emptyText: string
  selectedKey: string | null
  onSelect: (key: string) => void
}) {
  const reduce = useReducedMotion()
  const empty = groups.length === 0
  return (
    <div className="sk-list__rows" role="listbox" aria-label={t('skills.source.installed')}>
      <ListNotes loading={loading} error={error} empty={empty} emptyText={emptyText} />
      {groups.map((group) => (
        <section key={group.key} className="sk-group" aria-label={group.label}>
          <header className="sk-group__head" title={group.help}>
            <span className="sk-group__label">{group.label}</span>
            <span className="sk-group__count">{group.skills.length}</span>
          </header>
          <AnimatePresence initial={false}>
            {group.skills.map((skill) => (
              <InstalledRow
                key={skillRowKey(skill)}
                skill={skill}
                selected={selectedKey === skillRowKey(skill)}
                reduce={Boolean(reduce)}
                onSelect={() => onSelect(skillRowKey(skill))}
              />
            ))}
          </AnimatePresence>
        </section>
      ))}
    </div>
  )
}

function InstalledRow({
  skill,
  selected,
  reduce,
  onSelect,
}: {
  skill: RawSkill
  selected: boolean
  reduce: boolean
  onSelect: () => void
}) {
  const bucket = skillBucket(skill)
  const withheld = skill.availability?.offered === false
  return (
    <motion.button
      type="button"
      role="option"
      aria-selected={selected}
      className="sk-row app-no-drag"
      data-status={bucket}
      data-withheld={withheld ? 'true' : undefined}
      layout={!reduce}
      initial={reduce ? false : { opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      exit={reduce ? undefined : { opacity: 0, height: 0 }}
      transition={quick}
      onClick={onSelect}
    >
      <SkillMark skill={skill} />
      <span className="sk-row__body">
        <span className="sk-row__name">{skill.name}</span>
        <span className="sk-row__sub">{skill.description || ''}</span>
      </span>
      <span className="sk-row__light" title={skillDotTitle(skill)} aria-hidden />
    </motion.button>
  )
}

export function RegistryList({
  items,
  loading,
  error,
  emptyText,
  selectedKey,
  onSelect,
  label,
}: {
  items: RegistryItem[]
  loading: boolean
  error: string | null
  emptyText: string
  selectedKey: string | null
  onSelect: (key: string) => void
  label: string
}) {
  const reduce = useReducedMotion()
  return (
    <div className="sk-list__rows" role="listbox" aria-label={label}>
      <ListNotes loading={loading} error={error} empty={items.length === 0} emptyText={emptyText} />
      <AnimatePresence initial={false}>
        {items.map((item) => {
          const key = registryKey(item)
          const cat = item.category && item.category !== 'other' ? catLabel(item.category) : ''
          return (
            <motion.button
              key={key}
              type="button"
              role="option"
              aria-selected={selectedKey === key}
              className="sk-row app-no-drag"
              data-installed={item.installed ? 'true' : undefined}
              layout={!reduce}
              initial={reduce ? false : { opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduce ? undefined : { opacity: 0, height: 0 }}
              transition={quick}
              onClick={() => onSelect(key)}
            >
              <RegistryMark item={item} />
              <span className="sk-row__body">
                <span className="sk-row__name">{item.name}</span>
                <span className="sk-row__sub">
                  {item.provider || item.source || ''}
                  {cat ? ` · ${cat}` : ''}
                </span>
              </span>
              {item.installed ? (
                <span className="sk-row__installed" title={t('skills.action.installed')}>
                  <Check className="size-3.5" strokeWidth={2.25} aria-hidden />
                </span>
              ) : null}
            </motion.button>
          )
        })}
      </AnimatePresence>
    </div>
  )
}
