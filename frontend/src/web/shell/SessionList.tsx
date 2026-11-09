import {
  ChevronRight,
  Filter,
  GripVertical,
  Layers,
  Loader2,
  MoreHorizontal,
  Search,
  X,
} from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
  type ReactNode,
} from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webShell'
import { cn } from '@/lib/utils'
import type { HistoryResponse } from '@/views/chat/transcript/history'
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
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { ChannelIcon } from './channel-icon'
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
  sessionChannel,
  sessionIsRunning,
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

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-col gap-2 px-3 pt-3 pb-2">
        <div className="flex items-center gap-1">
          <h2 className="flex-1 text-small font-semibold tracking-wide text-text-muted uppercase">
            {t('webShell.sessionsHeading')}
          </h2>
          <GroupingMenu grouping={grouping} onChange={setGrouping} />
          <FilterMenu
            filters={filters}
            options={options}
            projectName={projectName}
            onChange={setFilters}
          />
        </div>
        <label className="relative block">
          <span className="sr-only">{t('webShell.sessionsFilter')}</span>
          <Search
            className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-text-muted"
            aria-hidden
          />
          <input
            type="search"
            value={filters.text}
            onChange={(e) => setFilters({ text: e.target.value })}
            placeholder={t('webShell.sessionsFilter')}
            className="h-8 w-full rounded-md border border-border-control bg-surface-0 pr-2 pl-8 text-ui placeholder:text-text-muted touch:h-11"
          />
        </label>
        {active ? (
          <ActiveFilters filters={filters} projectName={projectName} onClear={clearFilters} />
        ) : null}
      </div>

      <div className="scroll-contained min-h-0 flex-1 px-2 pb-3">
        {sessions.isPending ? (
          <LoadingRegion>
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="mx-1 my-1.5 h-7" />
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
              title={t('webShell.sessionsEmpty')}
              description={t('webShell.sessionsEmptyBody')}
            />
          )
        ) : (
          sections.map((section) => (
            <section key={section.id} aria-label={sectionTitle(section)} className="mt-2">
              <h3 className="px-2 py-1 text-small font-medium text-text-muted">
                {sectionTitle(section)}
              </h3>
              <ul role="list" className="flex flex-col">
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
          className="target inline-flex items-center gap-1 rounded-full bg-accent-soft px-2 text-small text-text"
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
  const [menuOpen, setMenuOpen] = useState(false)
  const actionsRef = useRef<HTMLButtonElement>(null)
  const href = sessionKeyToPath(row.key)

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

  const exportMarkdown = async () => {
    const history = await rpc.call<HistoryResponse>('chat.history', {
      sessionKey: row.key,
      limit: 500,
      includeCanonical: false,
      includeSummaries: false,
    })
    // The export builder and the reset flow ride in their own chunk: they are
    // rarely used and pull in the chat logic module.
    const { exportMarkdownDocument } = await import('@/views/chat/logic')
    const md = exportMarkdownDocument(
      (history?.messages ?? []).map((m) => ({ role: m.role, text: m.text, ts: m.timestamp })),
      row.key,
    )
    if (md === null) {
      toast.warning(t('webShell.exportEmpty'))
      return
    }
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([md], { type: 'text/markdown' }))
    a.download = `chat-${row.key}.md`
    a.click()
    URL.revokeObjectURL(a.href)
    toast.info(t('webShell.exported'))
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
        void run(t('webShell.actionReset'), async () => {
          const { resetSession } = await import('@/views/chat/resetSession')
          await resetSession(rpc, row.key)
        })
        return
      case 'export':
        void run(t('webShell.actionExport'), exportMarkdown)
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
      <Item onSelect={() => window.open(href, '_blank', 'noopener')}>
        {t('webShell.actionOpenNewTab')}
      </Item>
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
  return (
    <li data-session-key={row.key} {...dragProps}>
      <ContextMenu>
        <ContextMenuTrigger asChild>
          <div
            className={cn(
              'group relative flex items-center gap-1 rounded-md pr-1',
              current ? 'bg-surface-3' : 'hover:bg-surface-2',
            )}
            style={{ paddingLeft: depth * 14 }}
            onKeyDown={onRowKeyDown}
          >
            {pinnedIndex >= 0 ? (
              <GripVertical className="size-3.5 shrink-0 cursor-grab text-text-muted" aria-hidden />
            ) : null}
            {node.children.length > 0 ? (
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                aria-expanded={expanded}
                aria-label={expanded ? t('webShell.sessionCollapse') : t('webShell.sessionExpand')}
                className="target inline-flex shrink-0 items-center justify-center rounded text-text-muted hover:text-text"
              >
                <ChevronRight
                  className={cn('size-3.5 transition-transform', expanded && 'rotate-90')}
                  aria-hidden
                />
              </button>
            ) : null}
            {renaming ? (
              <form
                className="flex min-w-0 flex-1 items-center gap-1 py-1"
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
                  className="h-7 min-w-0 flex-1 rounded border border-border-control bg-surface-0 px-2 text-ui"
                />
                <Button size="sm" type="submit" disabled={busy}>
                  {t('webShell.renameSave')}
                </Button>
              </form>
            ) : (
              <Link
                to={href}
                onClick={(e) => {
                  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
                  onNavigate?.()
                }}
                aria-current={current ? 'page' : undefined}
                className="target flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-1.5 text-ui"
              >
                <ChannelIcon channel={sessionChannel(row)} className="shrink-0 text-text-muted" />
                <span className="min-w-0 flex-1 truncate">{title}</span>
                {row.provisional ? (
                  <span className="text-small text-text-muted">
                    {t('webShell.sessionProvisional')}
                  </span>
                ) : null}
                {running ? (
                  <span
                    role="img"
                    aria-label={t('webShell.sessionRunning')}
                    className="inline-flex shrink-0"
                  >
                    <Loader2
                      className="motion-status size-3.5 animate-spin text-accent"
                      aria-hidden
                      data-status-icon
                    />
                  </span>
                ) : null}
                {!expanded && node.childCount > 0 ? (
                  <span
                    className="shrink-0 rounded-full bg-surface-3 px-1.5 text-small text-text-muted"
                    title={tPlural('webShell.sessionChildren', node.childCount)}
                  >
                    <span className="sr-only">
                      {tPlural('webShell.sessionChildren', node.childCount)}
                      {node.childRunning ? `, ${t('webShell.sessionChildrenRunning')}` : ''}
                    </span>
                    <span aria-hidden>{node.childCount}</span>
                    {node.childRunning ? (
                      <span aria-hidden className="ml-0.5 text-accent">
                        •
                      </span>
                    ) : null}
                  </span>
                ) : null}
                <span className="shrink-0 text-small text-text-muted group-hover:hidden group-focus-within:hidden">
                  {relTimeLabel(row.updated_at ?? row.updatedAt ?? '')}
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
                    className="opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100 touch:opacity-100"
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
        <p id={`${row.key}-error`} role="alert" className="px-2 pb-1 text-small text-danger">
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
