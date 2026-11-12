import {
  ChevronRight,
  Filter,
  GripVertical,
  Layers,
  MessagesSquare,
  MoreHorizontal,
  Search,
  SearchX,
  X,
} from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
  type ReactNode,
} from 'react'
import { Link, useHref, useLocation, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webShell'
import { usePendingQuestions } from '@/lib/pending-questions'
import { cn } from '@/lib/utils'
import { relTimeLabel } from '@/views/sessions/logic'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/web/components/ui/alert-dialog'
import { ResetSessionDialog } from './ResetSessionDialog'
import { Button } from '@/web/components/ui/button'
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from '@/web/components/ui/context-menu'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'
import { Input } from '@/web/components/ui/input'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { ChannelIcon } from './channel-icon'
import { exportSessionMarkdown } from './session-actions'
import { SESSIONS_QUERY_KEY, useProjectNames, useSessionsList } from './session-data'
import {
  GROUPING_KEY,
  PINS_KEY,
  filterOptions,
  groupSessions,
  hasActiveFilters,
  matchesFilters,
  movePin,
  nestSessions,
  parsePins,
  reorderPins,
  sessionAgent,
  sessionChannel,
  sessionIsRunning,
  keyLabel,
  sessionTitle,
  togglePin,
  type SessionFilters,
  type SessionGrouping,
  type SessionNode,
  type SessionRow,
  type SessionSection,
} from './session-list'
import { pathToSessionKey, sessionKeyToPath } from './session-url'
import { useShell } from './shell-store'

// ── Row titles ──────────────────────────────────────────────────────────────

/**
 * The gateway's fallback "derived title" for an unnamed session is the short
 * opaque session id (8 hex characters): an identifier, not a title.
 */
function isOpaqueTitle(title: string): boolean {
  return /^[0-9a-f]{8}$/i.test(title.trim())
}

/**
 * How a list row names a session. A real name or derived title is the
 * headline; an unnamed session reads as untitled (the caller shows "New
 * conversation", as the chat header does) with its short id as a secondary
 * identifier, never the id as the headline.
 */
export function sessionHeadline(row: SessionRow): { title: string | null; id: string } {
  const display = String(row.display_name || row.displayName || '').trim()
  const derived = String(row.derived_title || row.derivedTitle || '').trim()
  const key = keyLabel(row.key)
  if (display) return { title: display, id: key }
  if (derived && !isOpaqueTitle(derived) && derived !== key) return { title: derived, id: key }
  return { title: null, id: derived && isOpaqueTitle(derived) ? derived : key }
}

// ── Browser-stored list preferences ─────────────────────────────────────────

function readGrouping(): SessionGrouping {
  try {
    return localStorage.getItem(GROUPING_KEY) === 'project' ? 'project' : 'date'
  } catch {
    return 'date'
  }
}

function readPins(): string[] {
  try {
    return parsePins(localStorage.getItem(PINS_KEY))
  } catch {
    return []
  }
}

function store(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* storage unavailable: the choice applies to this tab */
  }
}

export function usePins(): [string[], (next: string[]) => void] {
  const [pins, setPinsState] = useState<string[]>(readPins)
  const setPins = useCallback((next: string[]) => {
    setPinsState(next)
    store(PINS_KEY, JSON.stringify(next))
  }, [])
  return [pins, setPins]
}

function sectionTitle(section: SessionSection): string {
  switch (section.id) {
    case 'pinned':
      return t('webShell.sectionPinned')
    case 'today':
      return t('webShell.sectionToday')
    case 'yesterday':
      return t('webShell.sectionYesterday')
    case 'previous7':
      return t('webShell.sectionPrevious7')
    case 'older':
      return t('webShell.sectionOlder')
    case 'no-project':
      return t('webShell.sectionNoProject')
    default:
      return section.label ?? section.id
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

// ── The list ────────────────────────────────────────────────────────────────

// Rows rise in once, on the first load of this page only: re-opening the pane
// or switching back from Pages shows the list as it is.
let listEntered = false

/** Whether this mount should stagger its first rows in. */
function useFirstLoadStagger(ready: boolean): boolean {
  const [stagger, setStagger] = useState(() => !listEntered)
  useEffect(() => {
    if (!ready || !stagger) return
    listEntered = true
    // Past the longest entrance (300 ms + 5 steps of 30 ms); later rows just appear.
    const id = window.setTimeout(() => setStagger(false), 600)
    return () => window.clearTimeout(id)
  }, [ready, stagger])
  return stagger
}

function RowSkeleton({ wide }: { wide: boolean }) {
  return (
    <div className="flex items-center gap-2.5 px-2 py-1.5">
      <Skeleton className="size-7 shrink-0 rounded-md" />
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <Skeleton className={wide ? 'h-3 w-4/5' : 'h-3 w-3/5'} />
        <Skeleton className="h-2.5 w-1/3" />
      </div>
    </div>
  )
}

export function SessionList({ onNavigate }: { onNavigate?: () => void }) {
  const sessions = useSessionsList()
  const projects = useProjectNames()
  const filters = useShell((s) => s.filters)
  const setFilters = useShell((s) => s.setFilters)
  const clearFilters = useShell((s) => s.clearFilters)
  const [grouping, setGroupingState] = useState<SessionGrouping>(readGrouping)
  const [pins, setPins] = usePins()
  const { pathname } = useLocation()
  const currentKey = pathname.startsWith('/chat/') ? pathToSessionKey(pathname) : null

  const setGrouping = (next: string) => {
    const value: SessionGrouping = next === 'project' ? 'project' : 'date'
    setGroupingState(value)
    store(GROUPING_KEY, value)
  }

  const rows = sessions.rows
  const options = useMemo(() => filterOptions(rows), [rows])
  const sections = useMemo(() => {
    const visible = rows.filter((row) => matchesFilters(row, filters))
    return groupSessions(nestSessions(visible), {
      grouping,
      pins,
      projects: projects.data,
    })
  }, [rows, filters, grouping, pins, projects.data])

  const projectName = (id: string) => projects.data?.get(id) ?? id
  const active = hasActiveFilters(filters)
  const stagger = useFirstLoadStagger(!sessions.isPending && sections.length > 0)

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-col gap-2.5 px-3 pt-3 pb-2">
        <div className="flex h-7 items-center gap-0.5 pl-1">
          <h2 className="text-section">{t('webShell.sessionsHeading')}</h2>
          {rows.length > 0 ? (
            <span
              aria-hidden
              className="ml-2 rounded-full bg-surface-3 px-1.5 text-small leading-5 font-medium text-text-muted tabular-nums"
            >
              {rows.length}
            </span>
          ) : null}
          <span className="flex-1" />
          <GroupingMenu grouping={grouping} onChange={setGrouping} />
          <FilterMenu
            filters={filters}
            options={options}
            projectName={projectName}
            onChange={setFilters}
          />
        </div>
        <label className="block">
          <span className="sr-only">{t('webShell.sessionsFilter')}</span>
          <Input
            type="search"
            size="sm"
            icon={<Search />}
            value={filters.text}
            onChange={(e) => setFilters({ text: e.target.value })}
            placeholder={t('webShell.sessionsFilter')}
            className="touch:h-11"
          />
        </label>
        {active ? (
          <ActiveFilters filters={filters} projectName={projectName} onClear={clearFilters} />
        ) : null}
      </div>

      <div className="scroll-contained min-h-0 flex-1 px-2 pb-3">
        {sessions.isPending ? (
          <LoadingRegion className="pt-2">
            {Array.from({ length: 7 }, (_, i) => (
              <RowSkeleton key={i} wide={i % 3 !== 1} />
            ))}
          </LoadingRegion>
        ) : sessions.isError && rows.length === 0 ? (
          <ErrorState
            title={t('webShell.sessionsLoadFailed')}
            message={errorMessage(sessions.error)}
            onRetry={() => void sessions.refetch()}
          />
        ) : sections.length === 0 ? (
          active ? (
            <EmptyState
              size="sm"
              icon={<SearchX />}
              title={t('webShell.sessionsNoMatch')}
              description={<FilterSummary filters={filters} projectName={projectName} />}
              action={
                <Button size="sm" variant="outline" onClick={clearFilters}>
                  {t('webShell.sessionsClearFilters')}
                </Button>
              }
            />
          ) : (
            <EmptyState
              size="sm"
              icon={<MessagesSquare />}
              title={t('webShell.sessionsEmpty')}
              description={t('webShell.sessionsEmptyBody')}
            />
          )
        ) : (
          sections.map((section) => (
            <section
              key={section.id}
              aria-label={sectionTitle(section)}
              className="mt-4 first:mt-1"
            >
              <h3 className="pane-group-label">{sectionTitle(section)}</h3>
              <ul
                role="list"
                className={stagger ? 'anim-stagger flex flex-col gap-px' : 'flex flex-col gap-px'}
              >
                {section.nodes.map((node, index) => (
                  <SessionItem
                    key={node.row.key}
                    node={node}
                    depth={0}
                    currentKey={currentKey}
                    pins={pins}
                    setPins={setPins}
                    pinnedIndex={section.id === 'pinned' ? index : -1}
                    projects={projects.data}
                    onNavigate={onNavigate}
                  />
                ))}
              </ul>
            </section>
          ))
        )}
      </div>
    </div>
  )
}

function FilterSummary({
  filters,
  projectName,
}: {
  filters: SessionFilters
  projectName: (id: string) => string
}) {
  return (
    <span>
      {filterChips(filters, projectName)
        .map((c) => c.label)
        .join(' · ')}
    </span>
  )
}

function filterChips(filters: SessionFilters, projectName: (id: string) => string) {
  const chips: { key: keyof SessionFilters; label: string }[] = []
  if (filters.text.trim())
    chips.push({
      key: 'text',
      label: t('webShell.sessionsFilterChipText', { value: filters.text.trim() }),
    })
  if (filters.agent)
    chips.push({
      key: 'agent',
      label: t('webShell.sessionsFilterChipAgent', { value: filters.agent }),
    })
  if (filters.channel)
    chips.push({
      key: 'channel',
      label: t('webShell.sessionsFilterChipChannel', { value: filters.channel }),
    })
  if (filters.project)
    chips.push({
      key: 'project',
      label: t('webShell.sessionsFilterChipProject', { value: projectName(filters.project) }),
    })
  return chips
}

function ActiveFilters({
  filters,
  projectName,
  onClear,
}: {
  filters: SessionFilters
  projectName: (id: string) => string
  onClear: () => void
}) {
  const setFilters = useShell((s) => s.setFilters)
  return (
    <div
      role="group"
      aria-label={t('webShell.sessionsFilterActive')}
      className="flex flex-wrap items-center gap-1"
    >
      {filterChips(filters, projectName).map((chip) => (
        <button
          key={chip.key}
          type="button"
          onClick={() => setFilters({ [chip.key]: chip.key === 'text' ? '' : null })}
          className="press target inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent-tint px-2 text-small text-text hover:border-accent/60"
        >
          {chip.label}
          <X className="size-3" aria-hidden />
        </button>
      ))}
      <Button size="sm" variant="link" onClick={onClear} className="h-6 px-1">
        {t('webShell.sessionsClearFilters')}
      </Button>
    </div>
  )
}

function GroupingMenu({
  grouping,
  onChange,
}: {
  grouping: SessionGrouping
  onChange: (value: string) => void
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="icon-sm" variant="ghost" aria-label={t('webShell.sessionsGrouping')}>
          <Layers />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{t('webShell.sessionsGrouping')}</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={grouping} onValueChange={onChange}>
          <DropdownMenuRadioItem value="date">
            {t('webShell.sessionsGroupByDate')}
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="project">
            {t('webShell.sessionsGroupByProject')}
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function FilterMenu({
  filters,
  options,
  projectName,
  onChange,
}: {
  filters: SessionFilters
  options: { agents: string[]; channels: string[]; projects: string[] }
  projectName: (id: string) => string
  onChange: (patch: Partial<SessionFilters>) => void
}) {
  const group = (
    key: 'agent' | 'channel' | 'project',
    label: string,
    values: string[],
    name: (v: string) => string = (v) => v,
  ) => (
    <>
      <DropdownMenuLabel>{label}</DropdownMenuLabel>
      <DropdownMenuRadioGroup
        value={filters[key] ?? ''}
        onValueChange={(v) => onChange({ [key]: v || null })}
      >
        <DropdownMenuRadioItem value="">{t('webShell.sessionsFilterAny')}</DropdownMenuRadioItem>
        {values.map((v) => (
          <DropdownMenuRadioItem key={v} value={v}>
            {name(v)}
          </DropdownMenuRadioItem>
        ))}
      </DropdownMenuRadioGroup>
    </>
  )
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="icon-sm" variant="ghost" aria-label={t('webShell.sessionsFilterMenu')}>
          <Filter />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-h-96 overflow-y-auto">
        {group('agent', t('webShell.sessionsFilterAgent'), options.agents)}
        <DropdownMenuSeparator />
        {group('channel', t('webShell.sessionsFilterChannel'), options.channels)}
        {options.projects.length > 0 ? (
          <>
            <DropdownMenuSeparator />
            {group('project', t('webShell.sessionsFilterProject'), options.projects, projectName)}
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// ── One row ─────────────────────────────────────────────────────────────────

interface ItemProps {
  node: SessionNode
  depth: number
  currentKey: string | null
  pins: string[]
  setPins: (next: string[]) => void
  /** Index within the Pinned section, or -1. */
  pinnedIndex: number
  projects?: ReadonlyMap<string, string>
  onNavigate?: () => void
}

type ActionId =
  'rename' | 'pin' | 'move-up' | 'move-down' | 'project' | 'reset' | 'export' | 'copy' | 'delete'

/**
 * Open a console path in a new tab. Not `noopener`: a tab opened that way
 * starts with empty sessionStorage, so on a token gateway it would land on the
 * Connect screen. The opener link is cut right after instead, once the new
 * tab has its copy of this tab's sessionStorage.
 */
export function openInNewTab(url: string): void {
  const opened = window.open(url, '_blank')
  if (opened) opened.opener = null
}

function SessionItem(props: ItemProps) {
  const { node, depth, currentKey, pins, setPins, pinnedIndex, projects, onNavigate } = props
  const row = node.row
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const title = sessionTitle(row)
  const running = sessionIsRunning(row)
  const pinned = pins.includes(row.key)
  const [expanded, setExpanded] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState(title)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [confirmReset, setConfirmReset] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const actionsRef = useRef<HTMLButtonElement>(null)
  const href = sessionKeyToPath(row.key)
  // A new tab is a fresh document: it needs the full console path (router
  // basename included), not the router-relative one.
  const tabHref = useHref(href)

  const refresh = () => queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY })

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      await refresh()
      return true
    } catch (e) {
      setError(t('webShell.actionFailed', { action: label, message: errorMessage(e) }))
      return false
    } finally {
      setBusy(false)
    }
  }

  const submitRename = async () => {
    const name = renameValue.trim()
    setBusy(true)
    setError(null)
    try {
      await rpc.call('sessions.rename', { key: row.key, name })
      queryClient.setQueryData<SessionRow[]>(SESSIONS_QUERY_KEY, (prev) =>
        prev?.map((r) => (r.key === row.key ? { ...r, display_name: name, displayName: name } : r)),
      )
      setRenaming(false)
      void refresh()
    } catch (e) {
      setError(t('webShell.renameFailed', { message: errorMessage(e) }))
    } finally {
      setBusy(false)
    }
  }

  const onAction = (id: ActionId, projectId?: string | null) => {
    switch (id) {
      case 'rename':
        setRenameValue(title)
        setRenaming(true)
        return
      case 'pin':
        setPins(togglePin(pins, row.key))
        return
      case 'move-up':
        setPins(movePin(pins, row.key, -1))
        return
      case 'move-down':
        setPins(movePin(pins, row.key, 1))
        return
      case 'project':
        void run(t('webShell.actionMoveToProject'), () =>
          rpc.call('sessions.patch', { key: row.key, projectId: projectId ?? null }),
        )
        return
      case 'reset':
        setConfirmReset(true)
        return
      case 'export':
        void run(t('webShell.actionExport'), () => exportSessionMarkdown(rpc, row.key))
        return
      case 'copy':
        void navigator.clipboard
          ?.writeText(row.key)
          .then(() => toast.success(t('webShell.copiedKey')))
          .catch((e: unknown) =>
            setError(
              t('webShell.actionFailed', {
                action: t('webShell.actionCopyKey'),
                message: errorMessage(e),
              }),
            ),
          )
        return
      case 'delete':
        setConfirmDelete(true)
        return
    }
  }

  const confirmDeletion = async () => {
    setConfirmDelete(false)
    setBusy(true)
    setError(null)
    try {
      await rpc.call('sessions.delete', { key: row.key })
      usePendingQuestions.getState().clearSession(row.key)
      if (pinned) setPins(pins.filter((k) => k !== row.key))
      queryClient.setQueryData<SessionRow[]>(SESSIONS_QUERY_KEY, (prev) =>
        prev?.filter((r) => r.key !== row.key),
      )
      if (currentKey === row.key) navigate('/chat')
      void refresh()
    } catch (e) {
      setError(t('webShell.deleteFailed', { message: errorMessage(e) }))
    } finally {
      setBusy(false)
    }
  }

  const onRowKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'F10' && e.shiftKey) {
      e.preventDefault()
      setMenuOpen(true)
    }
  }

  // Drag reordering inside the Pinned section.
  const dragProps =
    pinnedIndex >= 0
      ? {
          draggable: true,
          onDragStart: (e: DragEvent) => {
            e.dataTransfer.setData('text/x-agentos-pin', row.key)
            e.dataTransfer.effectAllowed = 'move'
          },
          onDragOver: (e: DragEvent) => {
            if (e.dataTransfer.types.includes('text/x-agentos-pin')) e.preventDefault()
          },
          onDrop: (e: DragEvent) => {
            const fromKey = e.dataTransfer.getData('text/x-agentos-pin')
            const from = pins.indexOf(fromKey)
            if (from < 0 || fromKey === row.key) return
            e.preventDefault()
            setPins(reorderPins(pins, from, pins.indexOf(row.key)))
          },
        }
      : {}

  const menuItems = (
    Item: typeof DropdownMenuItem | typeof ContextMenuItem,
    Sep: () => ReactNode,
  ) => (
    <>
      <Item onSelect={() => openInNewTab(tabHref)}>{t('webShell.actionOpenNewTab')}</Item>
      <Item onSelect={() => onAction('rename')}>{t('webShell.actionRename')}</Item>
      <Item onSelect={() => onAction('pin')}>
        {pinned ? t('webShell.actionUnpin') : t('webShell.actionPin')}
      </Item>
      {pinned ? (
        <>
          <Item onSelect={() => onAction('move-up')}>{t('webShell.actionMoveUp')}</Item>
          <Item onSelect={() => onAction('move-down')}>{t('webShell.actionMoveDown')}</Item>
        </>
      ) : null}
      {projects && projects.size > 0 ? (
        <>
          {Sep()}
          <DropdownMenuLabel>{t('webShell.actionMoveToProject')}</DropdownMenuLabel>
          <Item onSelect={() => onAction('project', null)}>{t('webShell.actionNoProject')}</Item>
          {[...projects].map(([id, name]) => (
            <Item key={id} onSelect={() => onAction('project', id)}>
              {name}
            </Item>
          ))}
        </>
      ) : null}
      {Sep()}
      <Item onSelect={() => onAction('reset')}>{t('webShell.actionReset')}</Item>
      <Item onSelect={() => onAction('export')}>{t('webShell.actionExport')}</Item>
      <Item onSelect={() => onAction('copy')}>{t('webShell.actionCopyKey')}</Item>
      {Sep()}
      <Item variant="danger" onSelect={() => onAction('delete')}>
        {t('webShell.actionDelete')}
      </Item>
    </>
  )

  const current = currentKey === row.key
  // An unnamed session reads "New conversation" with its short id beside the
  // agent, as the chat header does; the id is never the headline.
  const headline = sessionHeadline(row)
  const channel = sessionChannel(row)
  const agent = sessionAgent(row)
  return (
    <li data-session-key={row.key} {...dragProps}>
      <ContextMenu>
        <ContextMenuTrigger asChild>
          <div
            data-current={current || undefined}
            className={cn(
              'session-row group relative flex items-center rounded-[10px] transition-colors duration-[var(--duration-fast)]',
              current ? 'bg-surface-3 shadow-highlight' : 'hover:bg-surface-2',
            )}
            style={{ paddingLeft: depth * 14 }}
            onKeyDown={onRowKeyDown}
          >
            {pinnedIndex >= 0 ? (
              <GripVertical
                className="-mr-1 ml-0.5 size-3.5 shrink-0 cursor-grab text-text-muted opacity-60 group-hover:opacity-100"
                aria-hidden
              />
            ) : null}
            {node.children.length > 0 ? (
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                aria-expanded={expanded}
                aria-label={expanded ? t('webShell.sessionCollapse') : t('webShell.sessionExpand')}
                className="target -mr-1 inline-flex shrink-0 items-center justify-center rounded text-text-muted hover:text-text"
              >
                <ChevronRight
                  className={cn(
                    'size-3.5 transition-transform duration-[var(--duration-base)] ease-[var(--ease-out)]',
                    expanded && 'rotate-90',
                  )}
                  aria-hidden
                />
              </button>
            ) : null}
            {renaming ? (
              <form
                className="flex min-w-0 flex-1 items-center gap-1 px-1.5 py-1.5"
                onSubmit={(e) => {
                  e.preventDefault()
                  void submitRename()
                }}
              >
                <input
                  autoFocus
                  aria-label={t('webShell.renameLabel')}
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') {
                      e.stopPropagation()
                      setRenaming(false)
                      setError(null)
                    }
                  }}
                  disabled={busy}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? `${row.key}-error` : undefined}
                  className="h-8 min-w-0 flex-1 rounded-md border border-border-control bg-surface-inset px-2 text-ui"
                />
                <Button size="sm" variant="primary" type="submit" disabled={busy}>
                  {t('webShell.renameSave')}
                </Button>
              </form>
            ) : (
              <Link
                to={href}
                viewTransition
                onClick={(e) => {
                  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
                  onNavigate?.()
                }}
                aria-current={current ? 'page' : undefined}
                className="target flex min-w-0 flex-1 items-center gap-2.5 rounded-[10px] py-[7px] pr-2.5 pl-2 text-ui"
              >
                <span
                  className={cn(
                    'session-tile relative grid size-7 shrink-0 place-items-center rounded-[9px]',
                    current && 'session-tile-current',
                  )}
                >
                  <ChannelIcon channel={channel} className="flex" />
                  {running ? (
                    <span
                      role="img"
                      aria-label={t('webShell.sessionRunning')}
                      className="absolute -top-0.5 -right-0.5 flex rounded-full bg-surface-1 p-[3px] text-accent"
                    >
                      <span
                        data-live
                        data-status-icon
                        className="status-dot motion-status size-1.5"
                      />
                    </span>
                  ) : null}
                </span>
                <span className="flex min-w-0 flex-1 flex-col gap-px">
                  <span className="flex min-w-0 items-baseline gap-2">
                    <span
                      className={cn(
                        'session-title min-w-0 flex-1 truncate',
                        headline.title === null && 'text-text-muted',
                        current && 'font-medium',
                      )}
                    >
                      {headline.title ?? t('webShell.sessionUntitled')}
                    </span>
                    <span className="shrink-0 text-small text-text-muted tabular-nums group-focus-within:invisible group-hover:invisible">
                      {relTimeLabel(row.updated_at ?? row.updatedAt ?? '')}
                    </span>
                  </span>
                  <span className="flex min-w-0 items-center gap-1.5 text-small leading-4 text-text-muted">
                    <span className="min-w-0 truncate">
                      {headline.title === null ? (
                        <span className="font-mono text-[11.5px] tracking-tight">
                          {headline.id}
                          <span aria-hidden>{' · '}</span>
                        </span>
                      ) : null}
                      {agent}
                      <span className="sr-only">
                        {' · '}
                        {channel}
                      </span>
                    </span>
                    {row.provisional ? (
                      <span className="shrink-0 text-accent">
                        {t('webShell.sessionProvisional')}
                      </span>
                    ) : null}
                    {!expanded && node.childCount > 0 ? (
                      <span
                        className="ml-auto inline-flex shrink-0 items-center gap-1 rounded-full bg-surface-3 px-1.5 leading-4 tabular-nums"
                        title={tPlural('webShell.sessionChildren', node.childCount)}
                      >
                        <span className="sr-only">
                          {tPlural('webShell.sessionChildren', node.childCount)}
                          {node.childRunning ? `, ${t('webShell.sessionChildrenRunning')}` : ''}
                        </span>
                        <span aria-hidden>{node.childCount}</span>
                        {node.childRunning ? (
                          <span aria-hidden className="status-dot size-1.5 text-accent" />
                        ) : null}
                      </span>
                    ) : null}
                  </span>
                </span>
              </Link>
            )}
            {!renaming && !row.provisional ? (
              <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
                <DropdownMenuTrigger asChild>
                  <Button
                    ref={actionsRef}
                    size="icon-sm"
                    variant="ghost"
                    aria-label={t('webShell.rowActions', { title })}
                    disabled={busy}
                    className="absolute top-1/2 right-1 -translate-y-1/2 opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 hover:bg-surface-overlay focus-visible:opacity-100 data-[state=open]:bg-surface-overlay data-[state=open]:opacity-100 touch:static touch:translate-y-0 touch:opacity-100"
                  >
                    <MoreHorizontal />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {menuItems(DropdownMenuItem, () => (
                    <DropdownMenuSeparator />
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            ) : null}
          </div>
        </ContextMenuTrigger>
        {row.provisional ? null : (
          <ContextMenuContent>
            {menuItems(ContextMenuItem, () => (
              <ContextMenuSeparator />
            ))}
          </ContextMenuContent>
        )}
      </ContextMenu>
      {error ? (
        <p
          id={`${row.key}-error`}
          role="alert"
          className="anim-fade-in px-2.5 pt-0.5 pb-1.5 text-small text-danger"
        >
          {error}
        </p>
      ) : null}
      {expanded && node.children.length > 0 ? (
        <ul role="list">
          {node.children.map((child) => (
            <SessionItem
              key={child.row.key}
              {...props}
              node={child}
              depth={depth + 1}
              pinnedIndex={-1}
            />
          ))}
        </ul>
      ) : null}
      <ResetSessionDialog
        open={confirmReset}
        onOpenChange={setConfirmReset}
        title={title}
        onCloseAutoFocus={(e) => {
          e.preventDefault()
          actionsRef.current?.focus()
        }}
        onConfirm={() =>
          void run(t('webShell.actionReset'), async () => {
            const { resetSession } = await import('@/views/chat/resetSession')
            await resetSession(rpc, row.key)
          })
        }
      />
      <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <AlertDialogContent
          onCloseAutoFocus={(e) => {
            e.preventDefault()
            actionsRef.current?.focus()
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>{t('webShell.deleteTitle', { title })}</AlertDialogTitle>
            <AlertDialogDescription>{t('webShell.deleteBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webCommon.cancel')}</AlertDialogCancel>
            <AlertDialogAction destructive onClick={() => void confirmDeletion()}>
              {t('webShell.deleteConfirm')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </li>
  )
}
