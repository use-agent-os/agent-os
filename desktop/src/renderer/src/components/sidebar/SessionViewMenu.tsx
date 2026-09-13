import { SlidersHorizontal } from 'lucide-react'
import { useCallback, useRef, useState } from 'react'
import { projectId, projectName, type RawProject } from '@/views/projects/logic'
import {
  MenuHeading,
  MenuItem,
  MenuSep,
  MenuSub,
  PopMenu,
  type MenuPlace,
} from '~/components/menu/PopMenu'
import { t, type MessageKey } from '~/i18n'
import { useSessionMarks } from '~/stores/session-marks'
import { useSessionView } from '~/stores/session-view'
import {
  GROUPINGS,
  isDefaultView,
  ORDERINGS,
  STATUS_FILTERS,
  type Grouping,
  type Ordering,
  type StatusFilter,
} from './logic'

export function groupingLabel(g: Grouping): string {
  return t(`view.grouping.${g}` as MessageKey)
}
export function orderingLabel(o: Ordering): string {
  return t(`view.ordering.${o}` as MessageKey)
}
export function statusLabel(s: StatusFilter): string {
  return t(`view.status.${s}` as MessageKey)
}

/**
 * The list's view menu, Mail's "View" as a popover: how rows group and
 * order, what shows, then the filters. The trigger lights up while the
 * view differs from the defaults so a narrowed list is never a mystery.
 */
export function SessionViewMenu({
  agents,
  projects,
}: {
  /** Agent ids seen across the list, for the Agent filter. */
  agents: string[]
  projects: RawProject[]
}) {
  const view = useSessionView((s) => s.view)
  const setView = useSessionView((s) => s.set)
  const reset = useSessionView((s) => s.reset)
  const unreadCount = useSessionMarks((s) => s.unread.size)
  const markAllRead = useSessionMarks((s) => s.markAllRead)
  const ref = useRef<HTMLButtonElement>(null)
  const [place, setPlace] = useState<MenuPlace | null>(null)
  const close = useCallback(() => setPlace(null), [])
  const custom = !isDefaultView(view)

  const filteredProject = projects.find((p) => projectId(p) === view.project)
  const projectValue =
    view.project === null
      ? undefined
      : view.project === ''
        ? t('view.project.none')
        : filteredProject
          ? projectName(filteredProject)
          : view.project

  return (
    <>
      <button
        ref={ref}
        type="button"
        className="mac-view-trigger app-no-drag"
        data-active={custom}
        aria-label={t('sidebar.filter')}
        aria-haspopup="menu"
        aria-expanded={place !== null}
        title={t('sidebar.filter')}
        onClick={(e) => {
          if (place) {
            close()
            return
          }
          setPlace({ anchor: e.currentTarget.getBoundingClientRect(), align: 'end' })
        }}
      >
        <SlidersHorizontal className="size-3.5" strokeWidth={1.75} aria-hidden />
      </button>
      {place ? (
        <PopMenu place={place} onClose={close} label={t('view.label')} triggerRef={ref}>
          <MenuSub label={t('view.grouping')} value={groupingLabel(view.grouping)}>
            {GROUPINGS.map((g) => (
              <MenuItem
                key={g}
                role="menuitemradio"
                checked={view.grouping === g}
                label={groupingLabel(g)}
                onSelect={() => setView({ grouping: g })}
              />
            ))}
          </MenuSub>
          <MenuSub label={t('view.ordering')} value={orderingLabel(view.ordering)}>
            {ORDERINGS.map((o) => (
              <MenuItem
                key={o}
                role="menuitemradio"
                checked={view.ordering === o}
                label={orderingLabel(o)}
                onSelect={() => setView({ ordering: o })}
              />
            ))}
          </MenuSub>
          <MenuSub label={t('view.show')}>
            <MenuItem
              checked={view.folders}
              label={t('view.show.folders')}
              onSelect={() => setView({ folders: !view.folders })}
            />
            <MenuItem
              checked={view.ages}
              label={t('view.show.ages')}
              onSelect={() => setView({ ages: !view.ages })}
            />
          </MenuSub>
          <MenuItem
            checked={view.inbox}
            label={t('view.inbox')}
            onSelect={() => setView({ inbox: !view.inbox })}
          />
          <MenuSep />
          <MenuHeading>{t('view.filters')}</MenuHeading>
          <MenuSub
            label={t('view.status')}
            value={view.status === 'all' ? undefined : statusLabel(view.status)}
          >
            {STATUS_FILTERS.map((s) => (
              <MenuItem
                key={s}
                role="menuitemradio"
                checked={view.status === s}
                label={statusLabel(s)}
                onSelect={() => setView({ status: s })}
              />
            ))}
          </MenuSub>
          <MenuSub label={t('view.agent')} value={view.agent ?? undefined}>
            <MenuItem
              role="menuitemradio"
              checked={view.agent === null}
              label={t('view.agent.all')}
              onSelect={() => setView({ agent: null })}
            />
            {agents.length > 0 ? <MenuSep /> : null}
            {agents.map((id) => (
              <MenuItem
                key={id}
                role="menuitemradio"
                checked={view.agent === id}
                label={id}
                onSelect={() => setView({ agent: id })}
              />
            ))}
          </MenuSub>
          <MenuSub label={t('view.project')} value={projectValue}>
            <MenuItem
              role="menuitemradio"
              checked={view.project === null}
              label={t('view.project.all')}
              onSelect={() => setView({ project: null })}
            />
            <MenuItem
              role="menuitemradio"
              checked={view.project === ''}
              label={t('view.project.none')}
              onSelect={() => setView({ project: '' })}
            />
            {projects.length > 0 ? <MenuSep /> : null}
            {projects.map((p) => {
              const id = projectId(p)
              return (
                <MenuItem
                  key={id}
                  role="menuitemradio"
                  checked={view.project === id}
                  label={projectName(p)}
                  onSelect={() => setView({ project: id })}
                />
              )
            })}
          </MenuSub>
          <MenuItem
            checked={view.archived}
            label={t('view.archived')}
            onSelect={() => setView({ archived: !view.archived })}
          />
          <MenuItem label={t('view.reset')} disabled={!custom} onSelect={reset} />
          <MenuSep />
          <MenuItem
            label={t('view.markAllRead')}
            disabled={unreadCount === 0}
            onSelect={markAllRead}
          />
        </PopMenu>
      ) : null}
    </>
  )
}
