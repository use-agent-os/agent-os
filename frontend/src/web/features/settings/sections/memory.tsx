import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BookOpen,
  Brain,
  Cpu,
  Database,
  FileText,
  FolderPlus,
  Layers,
  Link2,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  SearchX,
  Settings2,
  Sparkles,
  Trash2,
  Upload,
  User,
} from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webSetCapabilities'
import { gate, requireConnection, type Gate } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  filterMemoryFiles,
  formatBytes,
  formatScore,
  formatUsagePercent,
  isNearBudget,
  type CuratedMemoryData,
  type MemoryFileRow,
  type MemorySearchResultWire,
  type MemorySourceFilter,
  type MemoryStatusWire,
  type MemoryTab,
} from '@/views/memory/logic'
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
import { Badge } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { StatCard } from '@/web/components/ui/card'
import { IconTile, STAT_CARD_DENSE, StatWord } from '@/web/features/home/overview-ui'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { Input, Textarea } from '@/web/components/ui/input'
import { SegmentedControl } from '@/web/components/ui/segmented-control'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/web/components/ui/tabs'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useConfigEditor } from '../config-editor'
import { ConfigField, useRegisterFields } from '../fields'
import { SaveBar } from '../SaveBar'
import { Notice, SettingsCard } from '../ui'

// Settings › Memory (control-ui-settings "Memory"): curated MEMORY.md / USER.md
// entries, the knowledge base, the raw indexed sources, search, reindexing,
// and the embedding settings (config, explicit save). The memory RPCs act on
// the main agent, as the previous Memory page did.

const AGENT_ID = 'main'
const MEMORY_KEY = ['memory'] as const
const SOURCES: readonly MemorySourceFilter[] = ['all', 'memory', 'knowledge_base', 'sessions']
const EMBEDDING_PROVIDERS = [
  'auto',
  'none',
  'local',
  'openai',
  'openai-compatible',
  'ollama',
] as const

type Tab = MemoryTab | 'settings'
type Target = CuratedMemoryData['target']

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'object' && error !== null && 'message' in error) {
    return String((error as { message: unknown }).message)
  }
  return String(error)
}

function sourceLabel(source: MemorySourceFilter): string {
  return {
    all: t('webSetCapabilities.memSourceAll'),
    memory: t('webSetCapabilities.memSourceMemory'),
    knowledge_base: t('webSetCapabilities.memSourceKb'),
    sessions: t('webSetCapabilities.memSourceSessions'),
  }[source]
}

function fileSourceLabel(source?: string): string {
  const known = SOURCES.find((s) => s !== 'all' && s === (source || 'memory'))
  return known ? sourceLabel(known) : String(source)
}

function formatDate(iso?: string): string {
  return iso ? new Date(iso).toLocaleString() : t('webSetCapabilities.dash')
}

/** Calls a memory RPC for the main agent; failures surface as a toast. */
function useMemoryAction() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState<string | null>(null)
  const run = async <T,>(key: string, work: () => Promise<T>): Promise<T | undefined> => {
    setBusy(key)
    try {
      const result = await work()
      await queryClient.invalidateQueries({ queryKey: MEMORY_KEY })
      return result
    } catch (error) {
      toast.error(errorMessage(error))
      return undefined
    } finally {
      setBusy(null)
    }
  }
  return { rpc, busy, run }
}

type MemoryAction = ReturnType<typeof useMemoryAction>

export function MemorySection() {
  const rpc = useRpc()
  const connection = useDisplayedConnection()
  const blocked = gate(requireConnection(connection))
  const reasonId = useId()
  const action = useMemoryAction()
  // A settings-search jump to a config field lands on the tab that holds it.
  const [params] = useSearchParams()
  const [tab, setTab] = useState<Tab>(() =>
    params.get('field')?.startsWith('memory.') ? 'settings' : 'curated',
  )

  useRegisterFields('memory', [
    { id: 'memory.embedding.provider', label: t('webSetCapabilities.memEmbeddingProvider') },
    { id: 'memory.retrieval_mode', label: t('webSetCapabilities.memRetrievalMode') },
    { id: 'memory.inject_limit', label: t('webSetCapabilities.memInjectLimit') },
  ])

  // `memory.index` without force is an incremental sync that also reports the
  // index's counts; the previous Memory page read its status the same way.
  const status = useQuery({
    queryKey: [...MEMORY_KEY, 'status'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<MemoryStatusWire>('memory.index', { agentId: AGENT_ID, force: false })
    },
  })
  const kb = useKnowledgeBase()
  const curatedCount = useCuratedCount()

  const reindex = (force: boolean) =>
    action.run(force ? 'rebuild' : 'reindex', async () => {
      await rpc.call('memory.index', { agentId: AGENT_ID, force })
      toast.success(
        force ? t('webSetCapabilities.memRebuilt') : t('webSetCapabilities.memReindexed'),
      )
    })

  const describedBy = blocked.reason ? reasonId : undefined
  return (
    <div className="flex flex-col gap-4">
      {blocked.reason ? (
        <Notice id={reasonId} role="status" tone="warn" className="text-small">
          {blocked.reason}
        </Notice>
      ) : null}

      <div className="anim-stagger grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Tile
          icon={<Brain />}
          label={t('webSetCapabilities.memStatCurated')}
          value={curatedCount}
        />
        <Tile
          icon={<BookOpen />}
          label={t('webSetCapabilities.memStatDocuments')}
          value={kb.data?.length}
        />
        <Tile
          icon={<Layers />}
          label={t('webSetCapabilities.memStatChunks')}
          value={status.data?.chunkCount}
        />
        <Tile
          icon={<Cpu />}
          label={t('webSetCapabilities.memStatEngine')}
          value={
            status.data
              ? status.data.vecAvailable
                ? t('webSetCapabilities.memEngineVector')
                : t('webSetCapabilities.memEngineFts')
              : undefined
          }
        />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={blocked.disabled || action.busy === 'reindex'}
          aria-describedby={describedBy}
          onClick={() => void reindex(false)}
        >
          <RefreshCw
            aria-hidden
            className={action.busy === 'reindex' ? 'animate-spin' : undefined}
          />
          {action.busy === 'reindex'
            ? t('webSetCapabilities.memReindexing')
            : t('webSetCapabilities.memReindex')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={blocked.disabled || action.busy === 'rebuild'}
          aria-describedby={describedBy}
          onClick={() => void reindex(true)}
        >
          {action.busy === 'rebuild' ? (
            <Loader2 aria-hidden className="animate-spin" />
          ) : (
            <Database aria-hidden />
          )}
          {action.busy === 'rebuild'
            ? t('webSetCapabilities.memRebuilding')
            : t('webSetCapabilities.memRebuild')}
        </Button>
      </div>

      <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
        <TabsList aria-label={t('webSetCapabilities.memTabs')} className="flex-wrap">
          <TabsTrigger value="curated">
            <Brain className="size-4" aria-hidden />
            {t('webSetCapabilities.memTabCurated')}
          </TabsTrigger>
          <TabsTrigger value="knowledge_base">
            <BookOpen className="size-4" aria-hidden />
            {t('webSetCapabilities.memTabKb')}
          </TabsTrigger>
          <TabsTrigger value="raw_sources">
            <Database className="size-4" aria-hidden />
            {t('webSetCapabilities.memTabSources')}
          </TabsTrigger>
          <TabsTrigger value="search">
            <Search className="size-4" aria-hidden />
            {t('webSetCapabilities.memTabSearch')}
          </TabsTrigger>
          <TabsTrigger value="settings">
            <Settings2 className="size-4" aria-hidden />
            {t('webSetCapabilities.memTabSettings')}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="curated" className="anim-fade-in mt-5">
          <CuratedTab action={action} blocked={blocked} describedBy={describedBy} />
        </TabsContent>
        <TabsContent value="knowledge_base" className="anim-fade-in mt-5">
          <KnowledgeBaseTab action={action} blocked={blocked} describedBy={describedBy} />
        </TabsContent>
        <TabsContent value="raw_sources" className="anim-fade-in mt-5">
          <SourcesTab blocked={blocked} describedBy={describedBy} />
        </TabsContent>
        <TabsContent value="search" className="anim-fade-in mt-5">
          <SearchTab />
        </TabsContent>
        <TabsContent value="settings" className="anim-fade-in mt-5">
          <EmbeddingSettings />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function Tile({
  label,
  value,
  icon,
}: {
  label: string
  value?: number | string
  icon: React.ReactNode
}) {
  return (
    <StatCard
      className={STAT_CARD_DENSE}
      headingAs="p"
      icon={<IconTile>{icon}</IconTile>}
      label={label}
      value={
        typeof value === 'string' && value !== t('webSetCapabilities.dash') ? (
          <StatWord>{value}</StatWord>
        ) : (
          (value ?? t('webSetCapabilities.dash'))
        )
      }
    />
  )
}

// ── Curated memory ──────────────────────────────────────────────────────────

function useCurated(target: Target) {
  const rpc = useRpc()
  return useQuery({
    queryKey: [...MEMORY_KEY, 'curated', target],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<CuratedMemoryData>('memory.curated.get', { agentId: AGENT_ID, target })
    },
  })
}

function useCuratedCount(): number | undefined {
  const memory = useCurated('memory')
  const user = useCurated('user')
  if (!memory.data && !user.data) return undefined
  return (memory.data?.entries.length ?? 0) + (user.data?.entries.length ?? 0)
}

type CuratedEdit = { target: Target; old: string | null; text: string }

function CuratedTab({
  action,
  blocked,
  describedBy,
}: {
  action: MemoryAction
  blocked: Gate
  describedBy?: string
}) {
  const [edit, setEdit] = useState<CuratedEdit | null>(null)
  const [removing, setRemoving] = useState<{ target: Target; entry: string } | null>(null)
  const textId = useId()

  const save = async () => {
    if (!edit || !edit.text.trim()) return
    const content = edit.text.trim()
    const done = await action.run('curated', async () => {
      if (edit.old !== null) {
        await action.rpc.call('memory.curated.replace', {
          agentId: AGENT_ID,
          target: edit.target,
          oldText: edit.old,
          newContent: content,
        })
      } else {
        await action.rpc.call('memory.curated.add', {
          agentId: AGENT_ID,
          target: edit.target,
          content,
        })
      }
      toast.success(t('webSetCapabilities.memEntrySaved'))
      return true
    })
    if (done) setEdit(null)
  }

  const remove = (target: Target, entry: string) =>
    action.run('curated', async () => {
      await action.rpc.call('memory.curated.remove', {
        agentId: AGENT_ID,
        target,
        oldText: entry,
      })
      toast.success(t('webSetCapabilities.memEntryRemoved'))
    })

  return (
    <div className="grid gap-4 md:grid-cols-2">
      {(['memory', 'user'] as const).map((target) => (
        <CuratedPanel
          key={target}
          target={target}
          blocked={blocked}
          describedBy={describedBy}
          onAdd={() => setEdit({ target, old: null, text: '' })}
          onEdit={(entry) => setEdit({ target, old: entry, text: entry })}
          onRemove={(entry) => setRemoving({ target, entry })}
        />
      ))}

      <Dialog open={edit !== null} onOpenChange={(next) => (next ? undefined : setEdit(null))}>
        <DialogContent>
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault()
              void save()
            }}
          >
            <DialogHeader>
              <DialogTitle>
                {edit?.old !== null
                  ? t('webSetCapabilities.memEditEntry')
                  : t('webSetCapabilities.memAddEntry')}
              </DialogTitle>
              <DialogDescription>
                {edit?.target === 'user'
                  ? t('webSetCapabilities.memUserFile')
                  : t('webSetCapabilities.memAgentFile')}
              </DialogDescription>
            </DialogHeader>
            <label htmlFor={textId} className="font-medium">
              {t('webSetCapabilities.memEntryContent')}
            </label>
            <Textarea
              id={textId}
              rows={6}
              value={edit?.text ?? ''}
              onChange={(e) => setEdit((prev) => (prev ? { ...prev, text: e.target.value } : prev))}
            />
            <DialogFooter>
              <Button variant="ghost" onClick={() => setEdit(null)}>
                {t('webCommon.cancel')}
              </Button>
              <Button
                type="submit"
                variant="primary"
                disabled={blocked.disabled || !edit?.text.trim() || action.busy === 'curated'}
                aria-describedby={describedBy}
              >
                {action.busy === 'curated' ? <Loader2 className="animate-spin" /> : null}
                {t('webSetCapabilities.save')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={removing !== null}
        onOpenChange={(next) => (next ? undefined : setRemoving(null))}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('webSetCapabilities.memRemoveEntryTitle')}</AlertDialogTitle>
            <AlertDialogDescription className="line-clamp-4 break-words">
              {removing?.entry}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webCommon.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              destructive
              disabled={blocked.disabled}
              onClick={() => {
                if (removing) void remove(removing.target, removing.entry)
              }}
            >
              {t('webSetCapabilities.remove')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

function CuratedPanel({
  target,
  blocked,
  describedBy,
  onAdd,
  onEdit,
  onRemove,
}: {
  target: Target
  blocked: Gate
  describedBy?: string
  onAdd: () => void
  onEdit: (entry: string) => void
  onRemove: (entry: string) => void
}) {
  const query = useCurated(target)
  const data = query.data
  const title =
    target === 'memory'
      ? t('webSetCapabilities.memAgentTitle')
      : t('webSetCapabilities.memUserTitle')
  const percent = data ? formatUsagePercent(data.charCount, data.charLimit) : 0

  return (
    <section aria-label={title} className="set-card flex flex-col gap-3">
      <div className="flex items-start gap-3">
        <span
          aria-hidden
          className="grid size-8 shrink-0 place-items-center rounded-control bg-surface-2 text-text-muted shadow-highlight [&_svg]:size-4"
        >
          {target === 'memory' ? <Brain /> : <User />}
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="set-card-title">{title}</h2>
          <p className="text-small text-text-muted">
            {target === 'memory'
              ? t('webSetCapabilities.memAgentBody')
              : t('webSetCapabilities.memUserBody')}
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          disabled={blocked.disabled}
          aria-describedby={describedBy}
          onClick={onAdd}
        >
          <Plus aria-hidden />
          {t('webSetCapabilities.memAdd')}
        </Button>
      </div>
      {data ? (
        <div className="flex flex-col gap-1.5">
          <p className="text-small text-text-muted tabular-nums">
            {t('webSetCapabilities.memUsage', {
              used: data.charCount,
              limit: data.charLimit,
              percent,
            })}
          </p>
          <div
            role="meter"
            aria-label={t('webSetCapabilities.memBudget', { name: title })}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            className="h-1.5 overflow-hidden rounded-full bg-surface-inset shadow-inset"
          >
            <div
              className={cn(
                'h-full origin-left rounded-full transition-[scale] duration-[var(--duration-slow)] ease-[var(--ease-out)]',
                isNearBudget(data.charCount, data.charLimit)
                  ? 'bg-warn'
                  : 'bg-accent [background-image:var(--accent-gradient)]',
              )}
              style={{ scale: `${Math.min(percent, 100) / 100} 1` }}
            />
          </div>
        </div>
      ) : null}
      {query.isPending ? (
        <LoadingRegion className="flex flex-col gap-2">
          <Skeleton className="h-10 rounded-control" />
          <Skeleton className="h-10 rounded-control" />
        </LoadingRegion>
      ) : query.isError ? (
        <ErrorState
          title={t('webSetCapabilities.memCuratedFailed')}
          message={errorMessage(query.error)}
          onRetry={() => void query.refetch()}
        />
      ) : data && data.entries.length ? (
        <ul role="list" className="set-list flex flex-col shadow-none">
          {data.entries.map((entry, i) => (
            <li
              key={`${i}:${entry}`}
              className="group flex items-start gap-2 bg-surface-1 py-2 pr-1.5 pl-3 transition-colors duration-[var(--duration-fast)] hover:bg-surface-2"
            >
              <p className="min-w-0 flex-1 py-0.5 text-small break-words whitespace-pre-wrap">
                {entry}
              </p>
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={t('webSetCapabilities.memEditEntry')}
                disabled={blocked.disabled}
                aria-describedby={describedBy}
                onClick={() => onEdit(entry)}
              >
                <Pencil />
              </Button>
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={t('webSetCapabilities.memRemoveEntry')}
                className="hover:bg-danger/10 hover:text-danger"
                disabled={blocked.disabled}
                aria-describedby={describedBy}
                onClick={() => onRemove(entry)}
              >
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState
          size="sm"
          className="rounded-card border border-dashed border-border py-6"
          icon={target === 'memory' ? <Brain /> : <User />}
          title={t('webSetCapabilities.memCuratedEmpty')}
        />
      )}
    </section>
  )
}

// ── Knowledge base ──────────────────────────────────────────────────────────

function useKnowledgeBase() {
  const rpc = useRpc()
  return useQuery({
    queryKey: [...MEMORY_KEY, 'kb'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const res = await rpc.call<{ documents?: MemoryFileRow[] }>('memory.knowledge_base.list', {
        agentId: AGENT_ID,
      })
      return res?.documents ?? []
    },
  })
}

function KnowledgeBaseTab({
  action,
  blocked,
  describedBy,
}: {
  action: MemoryAction
  blocked: Gate
  describedBy?: string
}) {
  const docs = useKnowledgeBase()
  const [path, setPath] = useState('')
  const [removing, setRemoving] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const pathId = useId()
  const ingesting = action.busy === 'ingest'

  const ingestPath = async () => {
    const target = path.trim()
    if (!target) return
    const res = await action.run('ingest', () =>
      action.rpc.call<{ results?: { chunksIndexed?: number }[] }>('memory.knowledge_base.ingest', {
        agentId: AGENT_ID,
        path: target,
      }),
    )
    if (!res) return
    setPath('')
    const results = res.results ?? []
    toast.success(
      t('webSetCapabilities.memIngested', {
        documents: results.length,
        chunks: results.reduce((n, r) => n + (r.chunksIndexed ?? 0), 0),
      }),
    )
  }

  const ingestFiles = async (files: FileList | null) => {
    if (!files?.length) return
    const list = Array.from(files)
    const done = await action.run('ingest', async () => {
      for (const file of list) {
        await action.rpc.call('memory.knowledge_base.ingest', {
          agentId: AGENT_ID,
          filename: file.name,
          content: await file.text(),
        })
      }
      return true
    })
    if (done) toast.success(tPlural('webSetCapabilities.memUploaded', list.length))
    if (fileInput.current) fileInput.current.value = ''
  }

  const remove = (doc: string) =>
    action.run('kb-remove', async () => {
      await action.rpc.call('memory.knowledge_base.remove', { agentId: AGENT_ID, path: doc })
      toast.success(t('webSetCapabilities.memDocRemoved', { path: doc }))
    })

  return (
    <div className="flex flex-col gap-4">
      <form
        className="set-card flex flex-col gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          void ingestPath()
        }}
      >
        <label htmlFor={pathId} className="set-card-title">
          {t('webSetCapabilities.memIngestLabel')}
        </label>
        <p className="text-small text-text-muted">{t('webSetCapabilities.memIngestHelp')}</p>
        <div className="mt-1 flex flex-wrap gap-2">
          <Input
            id={pathId}
            icon={<Link2 />}
            wrapperClassName="min-w-48 flex-1"
            value={path}
            autoComplete="off"
            placeholder={t('webSetCapabilities.memIngestPlaceholder')}
            onChange={(e) => setPath(e.target.value)}
          />
          <Button
            type="submit"
            variant="primary"
            disabled={blocked.disabled || ingesting || !path.trim()}
            aria-describedby={describedBy}
          >
            {ingesting ? (
              <Loader2 aria-hidden className="animate-spin" />
            ) : (
              <FolderPlus aria-hidden />
            )}
            {ingesting ? t('webSetCapabilities.memIngesting') : t('webSetCapabilities.memIngest')}
          </Button>
          <Button
            variant="outline"
            disabled={blocked.disabled || ingesting}
            aria-describedby={describedBy}
            onClick={() => fileInput.current?.click()}
          >
            <Upload aria-hidden />
            {t('webSetCapabilities.memUpload')}
          </Button>
          <input
            ref={fileInput}
            type="file"
            multiple
            hidden
            aria-label={t('webSetCapabilities.memUpload')}
            onChange={(e) => void ingestFiles(e.target.files)}
          />
        </div>
      </form>

      {docs.isPending ? (
        <LoadingRegion className="set-list flex flex-col">
          {[0, 1, 2].map((i) => (
            <div key={i} className="flex items-center gap-3 px-4 py-3">
              <Skeleton className="size-8 rounded-control" />
              <div className="flex flex-1 flex-col gap-1.5">
                <Skeleton className="h-3.5 w-1/2" />
                <Skeleton className="h-3 w-1/3" />
              </div>
            </div>
          ))}
        </LoadingRegion>
      ) : docs.isError ? (
        <ErrorState
          title={t('webSetCapabilities.memKbFailed')}
          message={errorMessage(docs.error)}
          onRetry={() => void docs.refetch()}
        />
      ) : docs.data.length === 0 ? (
        <EmptyState
          icon={<BookOpen />}
          title={t('webSetCapabilities.memKbEmpty')}
          className="set-card py-8"
        />
      ) : (
        <FileList
          files={docs.data}
          label={t('webSetCapabilities.memTabKb')}
          action={(doc) => (
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label={t('webSetCapabilities.memRemoveDoc', { path: doc.path })}
              className="hover:bg-danger/10 hover:text-danger"
              disabled={blocked.disabled}
              aria-describedby={describedBy}
              onClick={() => setRemoving(doc.path)}
            >
              <Trash2 />
            </Button>
          )}
        />
      )}

      <AlertDialog
        open={removing !== null}
        onOpenChange={(next) => (next ? undefined : setRemoving(null))}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('webSetCapabilities.memRemoveDocTitle', { path: String(removing) })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t('webSetCapabilities.memRemoveDocBody')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webCommon.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              destructive
              disabled={blocked.disabled}
              onClick={() => {
                if (removing) void remove(removing)
              }}
            >
              {t('webSetCapabilities.remove')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

/** Indexed files as rows: path, source, lines, size, modified, and one action. */
function FileList({
  files,
  label,
  showSource = false,
  action,
}: {
  files: MemoryFileRow[]
  label: string
  showSource?: boolean
  action: (file: MemoryFileRow) => React.ReactNode
}) {
  return (
    <ul role="list" aria-label={label} className="set-list anim-stagger flex flex-col">
      {files.map((file) => (
        <li
          key={file.path}
          className="flex items-center gap-3 px-4 py-2.5 transition-colors duration-[var(--duration-fast)] hover:bg-surface-2"
        >
          <span
            aria-hidden
            className="grid size-8 shrink-0 place-items-center rounded-control bg-surface-2 text-text-muted"
          >
            <FileText className="size-4" />
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate font-mono text-small" title={file.path}>
              {file.path}
            </p>
            <p className="text-small text-text-muted tabular-nums">
              {showSource ? `${fileSourceLabel(file.source)} · ` : ''}
              {t('webSetCapabilities.memFileMeta', {
                lines: file.lineCount ?? t('webSetCapabilities.dash'),
                size: formatBytes(file.sizeBytes),
                modified: formatDate(file.modifiedAt),
              })}
            </p>
          </div>
          {action(file)}
        </li>
      ))}
    </ul>
  )
}

// ── Raw sources ─────────────────────────────────────────────────────────────

function SourcesTab({ blocked, describedBy }: { blocked: Gate; describedBy?: string }) {
  const rpc = useRpc()
  const [filter, setFilter] = useState('')
  const [source, setSource] = useState<MemorySourceFilter>('all')
  const [viewing, setViewing] = useState<string | null>(null)
  const filterId = useId()
  const files = useQuery({
    queryKey: [...MEMORY_KEY, 'raw'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const res = await rpc.call<{ files?: MemoryFileRow[] }>('memory.list', {
        agentId: AGENT_ID,
        source: 'all',
      })
      return res?.files ?? []
    },
  })
  const content = useQuery({
    queryKey: [...MEMORY_KEY, 'show', viewing],
    enabled: viewing !== null,
    queryFn: async () => {
      const res = await rpc.call<{ content?: string }>('memory.show', {
        agentId: AGENT_ID,
        path: viewing,
      })
      return res?.content ?? ''
    },
  })
  const shown = filterMemoryFiles(files.data ?? [], filter, source)

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={filterId} className="sr-only">
          {t('webSetCapabilities.memFilterLabel')}
        </label>
        <Input
          id={filterId}
          type="search"
          icon={<Search />}
          wrapperClassName="min-w-48 flex-1"
          value={filter}
          placeholder={t('webSetCapabilities.memFilterLabel')}
          onChange={(e) => setFilter(e.target.value)}
        />
        <SourceFilter value={source} onChange={setSource} />
      </div>
      {files.isPending ? (
        <LoadingRegion className="set-list flex flex-col">
          {[0, 1, 2].map((i) => (
            <div key={i} className="flex items-center gap-3 px-4 py-3">
              <Skeleton className="size-8 rounded-control" />
              <div className="flex flex-1 flex-col gap-1.5">
                <Skeleton className="h-3.5 w-1/2" />
                <Skeleton className="h-3 w-1/3" />
              </div>
            </div>
          ))}
        </LoadingRegion>
      ) : files.isError ? (
        <ErrorState
          title={t('webSetCapabilities.memSourcesFailed')}
          message={errorMessage(files.error)}
          onRetry={() => void files.refetch()}
        />
      ) : shown.length === 0 ? (
        <EmptyState
          icon={<Database />}
          title={t('webSetCapabilities.memSourcesEmpty')}
          className="set-card py-8"
        />
      ) : (
        <FileList
          files={shown}
          showSource
          label={t('webSetCapabilities.memTabSources')}
          action={(file) => (
            <Button
              size="sm"
              variant="outline"
              disabled={blocked.disabled}
              aria-describedby={describedBy}
              aria-label={t('webSetCapabilities.memViewNamed', { path: file.path })}
              onClick={() => setViewing(file.path)}
            >
              {t('webSetCapabilities.memView')}
            </Button>
          )}
        />
      )}

      <Dialog
        open={viewing !== null}
        onOpenChange={(next) => (next ? undefined : setViewing(null))}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle className="font-mono break-all">{viewing}</DialogTitle>
            <DialogDescription>{t('webSetCapabilities.memViewBody')}</DialogDescription>
          </DialogHeader>
          {content.isPending ? (
            <LoadingRegion>
              <Skeleton className="h-40" delay={0} />
            </LoadingRegion>
          ) : content.isError ? (
            <ErrorState
              title={t('webSetCapabilities.memViewFailed')}
              message={errorMessage(content.error)}
              onRetry={() => void content.refetch()}
            />
          ) : (
            <pre
              tabIndex={0}
              className="max-h-[60vh] overflow-auto rounded-control border border-border bg-surface-inset p-3 font-mono text-small whitespace-pre-wrap shadow-inset"
            >
              {content.data}
            </pre>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}

function SourceFilter({
  value,
  onChange,
}: {
  value: MemorySourceFilter
  onChange: (source: MemorySourceFilter) => void
}) {
  return (
    <SegmentedControl
      kind="toggle"
      size="sm"
      aria-label={t('webSetCapabilities.memSourceFilter')}
      value={value}
      onChange={onChange}
      options={SOURCES.map((source) => ({ value: source, label: sourceLabel(source) }))}
    />
  )
}

// ── Search ──────────────────────────────────────────────────────────────────

function SearchTab() {
  const rpc = useRpc()
  const [text, setText] = useState('')
  const [query, setQuery] = useState('')
  const [source, setSource] = useState<MemorySourceFilter>('all')
  const [minScore, setMinScore] = useState(0.35)
  const queryId = useId()
  const scoreId = useId()

  useEffect(() => {
    const id = window.setTimeout(() => setQuery(text.trim()), 300)
    return () => window.clearTimeout(id)
  }, [text])

  const results = useQuery({
    queryKey: [...MEMORY_KEY, 'search', query, source, minScore],
    enabled: query.length > 0,
    queryFn: async () => {
      await rpc.waitForConnection()
      const res = await rpc.call<{ results?: MemorySearchResultWire[] }>('memory.search', {
        agentId: AGENT_ID,
        query,
        source,
        minScore,
        limit: 15,
      })
      return res?.results ?? []
    },
  })

  return (
    <div className="flex flex-col gap-3">
      <label htmlFor={queryId} className="sr-only">
        {t('webSetCapabilities.memSearchLabel')}
      </label>
      <Input
        id={queryId}
        type="search"
        size="lg"
        icon={<Search />}
        value={text}
        placeholder={t('webSetCapabilities.memSearchPlaceholder')}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="flex flex-wrap items-center gap-3">
        <SourceFilter value={source} onChange={setSource} />
        <span className="flex-1" />
        <label htmlFor={scoreId} className="text-small text-text-muted tabular-nums">
          {t('webSetCapabilities.memMinScore', { score: formatScore(minScore) })}
        </label>
        <input
          id={scoreId}
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={minScore}
          onChange={(e) => setMinScore(Number(e.target.value))}
          className="w-28 accent-[var(--color-accent)]"
        />
      </div>
      {!query ? null : results.isPending ? (
        <LoadingRegion className="flex flex-col gap-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-20 rounded-card" />
          ))}
        </LoadingRegion>
      ) : results.isError ? (
        <ErrorState
          title={t('webSetCapabilities.memSearchFailed')}
          message={errorMessage(results.error)}
          onRetry={() => void results.refetch()}
        />
      ) : results.data.length === 0 ? (
        <EmptyState
          icon={<SearchX />}
          title={t('webSetCapabilities.memSearchEmpty', { query })}
          className="set-card py-8"
        />
      ) : (
        <ul
          role="list"
          aria-label={t('webSetCapabilities.memSearchResults')}
          className="anim-stagger flex flex-col gap-2"
        >
          {results.data.map((r) => (
            <li key={r.chunkId || `${r.path}:${r.startLine}`} className="set-card px-4 py-3">
              <div className="flex flex-wrap items-center gap-2 text-small">
                <FileText className="size-3.5 shrink-0 text-text-muted" aria-hidden />
                <span className="min-w-0 flex-1 truncate font-mono">{r.path}</span>
                <Badge tone="accent" className="tabular-nums">
                  {t('webSetCapabilities.memScore', { score: formatScore(r.score) })}
                </Badge>
                {r.startLine !== undefined && r.endLine !== undefined ? (
                  <span className="text-text-muted tabular-nums">
                    {t('webSetCapabilities.memLines', { start: r.startLine, end: r.endLine })}
                  </span>
                ) : null}
              </div>
              <p className="mt-2 border-l-2 border-border-strong pl-3 text-small whitespace-pre-wrap text-text-muted">
                {r.snippet}
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ── Embedding and memory settings (config) ──────────────────────────────────

function EmbeddingSettings() {
  const editor = useConfigEditor()
  if (!editor.ready) {
    return (
      <LoadingRegion className="flex flex-col gap-4">
        <Skeleton className="h-56 rounded-card" />
        <Skeleton className="h-40 rounded-card" />
      </LoadingRegion>
    )
  }
  const provider = String(editor.value('memory.embedding.provider') ?? 'auto')
  const remote = ['auto', 'openai', 'openai-compatible'].includes(provider)
  const field = (path: string, label: string, help?: string) => ({ editor, path, label, help })

  return (
    <div className="flex flex-col gap-4">
      <SettingsCard
        title={t('webSetCapabilities.memEmbeddingTitle')}
        description={t('webSetCapabilities.memEmbeddingBody')}
        icon={<Sparkles />}
      >
        <ConfigField
          {...field(
            'memory.embedding.provider',
            t('webSetCapabilities.memEmbeddingProvider'),
            t('webSetCapabilities.memEmbeddingProviderHelp'),
          )}
          type={{ kind: 'enum', options: EMBEDDING_PROVIDERS }}
        />
        {remote ? (
          <>
            <ConfigField
              {...field('memory.embedding.remote.base_url', t('webSetCapabilities.memRemoteUrl'))}
              optional
              type={{ kind: 'string' }}
            />
            <ConfigField
              {...field('memory.embedding.remote.model', t('webSetCapabilities.memModel'))}
              optional
              type={{ kind: 'string' }}
            />
            <ConfigField
              {...field(
                'memory.embedding.remote.api_key',
                t('webSetCapabilities.memApiKey'),
                t('webSetCapabilities.memApiKeyHelp'),
              )}
              type={{ kind: 'string', secret: true }}
            />
            <ConfigField
              {...field(
                'memory.embedding.remote.api_key_env',
                t('webSetCapabilities.memApiKeyEnv'),
              )}
              optional
              type={{ kind: 'string' }}
            />
            <ConfigField
              {...field(
                'memory.embedding.remote.dimensions',
                t('webSetCapabilities.memDimensions'),
              )}
              type={{ kind: 'number' }}
            />
          </>
        ) : null}
        {provider === 'ollama' ? (
          <>
            <ConfigField
              {...field('memory.embedding.ollama.base_url', t('webSetCapabilities.memOllamaUrl'))}
              optional
              type={{ kind: 'string' }}
            />
            <ConfigField
              {...field('memory.embedding.ollama.model', t('webSetCapabilities.memModel'))}
              optional
              type={{ kind: 'string' }}
            />
          </>
        ) : null}
        {provider === 'local' ? (
          <ConfigField
            {...field('memory.embedding.local.model', t('webSetCapabilities.memLocalModel'))}
            optional
            type={{ kind: 'string' }}
          />
        ) : null}
      </SettingsCard>
      <SettingsCard title={t('webSetCapabilities.memRetrievalTitle')} icon={<Search />}>
        <ConfigField
          {...field(
            'memory.retrieval_mode',
            t('webSetCapabilities.memRetrievalMode'),
            t('webSetCapabilities.memRetrievalModeHelp'),
          )}
          type={{ kind: 'enum', options: ['hybrid', 'fts_only'] }}
        />
        <ConfigField
          {...field(
            'memory.inject_limit',
            t('webSetCapabilities.memInjectLimit'),
            t('webSetCapabilities.memInjectLimitHelp'),
          )}
          type={{ kind: 'number' }}
        />
        <ConfigField
          {...field('memory.curated_memory_char_limit', t('webSetCapabilities.memAgentLimit'))}
          type={{ kind: 'number' }}
        />
        <ConfigField
          {...field('memory.curated_user_char_limit', t('webSetCapabilities.memUserLimit'))}
          type={{ kind: 'number' }}
        />
      </SettingsCard>
      <SaveBar editor={editor} />
    </div>
  )
}
