import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BookOpen,
  Brain,
  Database,
  FileText,
  FolderPlus,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  Trash2,
  TriangleAlert,
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
import { Button } from '@/web/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/web/components/ui/tabs'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useConfigEditor } from '../config-editor'
import { ConfigField, useRegisterFields } from '../fields'
import { SaveBar } from '../SaveBar'

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

const inputClass =
  'h-9 w-full rounded-md border border-border-control bg-surface-0 px-3 touch:h-11 touch:text-base'

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
        <p
          id={reasonId}
          role="status"
          className="flex items-center gap-2 rounded-md border border-warn/50 bg-warn/10 px-3 py-2 text-small"
        >
          <TriangleAlert className="size-4 shrink-0 text-warn" aria-hidden data-status-icon />
          {blocked.reason}
        </p>
      ) : null}

      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Tile label={t('webSetCapabilities.memStatCurated')} value={curatedCount} />
        <Tile label={t('webSetCapabilities.memStatDocuments')} value={kb.data?.length} />
        <Tile label={t('webSetCapabilities.memStatChunks')} value={status.data?.chunkCount} />
        <Tile
          label={t('webSetCapabilities.memStatEngine')}
          value={
            status.data
              ? status.data.vecAvailable
                ? t('webSetCapabilities.memEngineVector')
                : t('webSetCapabilities.memEngineFts')
              : undefined
          }
        />
      </dl>

      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={blocked.disabled || action.busy === 'reindex'}
          aria-describedby={describedBy}
          onClick={() => void reindex(false)}
        >
          <RefreshCw aria-hidden />
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
          <Database aria-hidden />
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
        <TabsContent value="curated" className="mt-4">
          <CuratedTab action={action} blocked={blocked} describedBy={describedBy} />
        </TabsContent>
        <TabsContent value="knowledge_base" className="mt-4">
          <KnowledgeBaseTab action={action} blocked={blocked} describedBy={describedBy} />
        </TabsContent>
        <TabsContent value="raw_sources" className="mt-4">
          <SourcesTab blocked={blocked} describedBy={describedBy} />
        </TabsContent>
        <TabsContent value="search" className="mt-4">
          <SearchTab />
        </TabsContent>
        <TabsContent value="settings" className="mt-4">
          <EmbeddingSettings />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function Tile({ label, value }: { label: string; value?: number | string }) {
  return (
    <div className="rounded-lg border border-border px-3 py-2">
      <dt className="text-small text-text-muted">{label}</dt>
      <dd className="text-lg font-semibold">{value ?? t('webSetCapabilities.dash')}</dd>
    </div>
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
            <textarea
              id={textId}
              rows={6}
              className={cn(inputClass, 'h-auto py-2')}
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
    <section aria-label={title} className="flex flex-col gap-2 rounded-lg border border-border p-3">
      <div className="flex items-start gap-2">
        {target === 'memory' ? (
          <Brain className="mt-0.5 size-4 text-text-muted" aria-hidden />
        ) : (
          <User className="mt-0.5 size-4 text-text-muted" aria-hidden />
        )}
        <div className="min-w-0 flex-1">
          <h2 className="font-semibold">{title}</h2>
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
        <div className="flex flex-col gap-1">
          <p className="text-small text-text-muted">
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
            className="h-1.5 overflow-hidden rounded-full bg-surface-3"
          >
            <div
              className={cn(
                'h-full rounded-full',
                isNearBudget(data.charCount, data.charLimit) ? 'bg-warn' : 'bg-accent',
              )}
              style={{ width: `${percent}%` }}
            />
          </div>
        </div>
      ) : null}
      {query.isPending ? (
        <LoadingRegion>
          <Skeleton className="h-20" />
        </LoadingRegion>
      ) : query.isError ? (
        <ErrorState
          title={t('webSetCapabilities.memCuratedFailed')}
          message={errorMessage(query.error)}
          onRetry={() => void query.refetch()}
        />
      ) : data && data.entries.length ? (
        <ul role="list" className="flex flex-col divide-y divide-border">
          {data.entries.map((entry, i) => (
            <li key={`${i}:${entry}`} className="flex items-start gap-2 py-2">
              <p className="min-w-0 flex-1 text-small break-words whitespace-pre-wrap">{entry}</p>
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
        <p className="py-4 text-center text-small text-text-muted">
          {t('webSetCapabilities.memCuratedEmpty')}
        </p>
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
        className="flex flex-col gap-2 rounded-lg border border-border p-3"
        onSubmit={(e) => {
          e.preventDefault()
          void ingestPath()
        }}
      >
        <label htmlFor={pathId} className="font-medium">
          {t('webSetCapabilities.memIngestLabel')}
        </label>
        <p className="text-small text-text-muted">{t('webSetCapabilities.memIngestHelp')}</p>
        <div className="flex flex-wrap gap-2">
          <input
            id={pathId}
            className={cn(inputClass, 'min-w-48 flex-1')}
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
            <FolderPlus aria-hidden />
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
        <LoadingRegion>
          <Skeleton className="h-24" />
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
          className="py-8"
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
    <ul
      role="list"
      aria-label={label}
      className="divide-y divide-border rounded-lg border border-border"
    >
      {files.map((file) => (
        <li key={file.path} className="flex items-center gap-3 px-3 py-2">
          <FileText className="size-4 shrink-0 text-text-muted" aria-hidden />
          <div className="min-w-0 flex-1">
            <p className="truncate font-mono text-small" title={file.path}>
              {file.path}
            </p>
            <p className="text-small text-text-muted">
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
        <input
          id={filterId}
          type="search"
          className={cn(inputClass, 'min-w-48 flex-1')}
          value={filter}
          placeholder={t('webSetCapabilities.memFilterLabel')}
          onChange={(e) => setFilter(e.target.value)}
        />
        <SourceFilter value={source} onChange={setSource} />
      </div>
      {files.isPending ? (
        <LoadingRegion>
          <Skeleton className="h-24" />
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
          className="py-8"
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
              className="max-h-[60vh] overflow-auto rounded-md bg-surface-0 p-3 font-mono text-small whitespace-pre-wrap"
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
    <div
      role="group"
      aria-label={t('webSetCapabilities.memSourceFilter')}
      className="flex flex-wrap gap-1"
    >
      {SOURCES.map((source) => (
        <Button
          key={source}
          size="sm"
          variant={value === source ? 'secondary' : 'ghost'}
          aria-pressed={value === source}
          onClick={() => onChange(source)}
        >
          {sourceLabel(source)}
        </Button>
      ))}
    </div>
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
      <input
        id={queryId}
        type="search"
        className={inputClass}
        value={text}
        placeholder={t('webSetCapabilities.memSearchPlaceholder')}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="flex flex-wrap items-center gap-3">
        <SourceFilter value={source} onChange={setSource} />
        <span className="flex-1" />
        <label htmlFor={scoreId} className="text-small text-text-muted">
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
        <LoadingRegion>
          <Skeleton className="h-24" />
        </LoadingRegion>
      ) : results.isError ? (
        <ErrorState
          title={t('webSetCapabilities.memSearchFailed')}
          message={errorMessage(results.error)}
          onRetry={() => void results.refetch()}
        />
      ) : results.data.length === 0 ? (
        <EmptyState title={t('webSetCapabilities.memSearchEmpty', { query })} className="py-8" />
      ) : (
        <ul
          role="list"
          aria-label={t('webSetCapabilities.memSearchResults')}
          className="flex flex-col gap-2"
        >
          {results.data.map((r) => (
            <li
              key={r.chunkId || `${r.path}:${r.startLine}`}
              className="rounded-lg border border-border p-3"
            >
              <div className="flex flex-wrap items-center gap-2 text-small">
                <span className="min-w-0 flex-1 truncate font-mono">{r.path}</span>
                <span className="rounded-full border border-border px-2 py-0.5">
                  {t('webSetCapabilities.memScore', { score: formatScore(r.score) })}
                </span>
                {r.startLine !== undefined && r.endLine !== undefined ? (
                  <span className="text-text-muted">
                    {t('webSetCapabilities.memLines', { start: r.startLine, end: r.endLine })}
                  </span>
                ) : null}
              </div>
              <p className="mt-1 text-small whitespace-pre-wrap text-text-muted">{r.snippet}</p>
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
      <LoadingRegion>
        <Skeleton className="h-40" />
      </LoadingRegion>
    )
  }
  const provider = String(editor.value('memory.embedding.provider') ?? 'auto')
  const remote = ['auto', 'openai', 'openai-compatible'].includes(provider)
  const field = (path: string, label: string, help?: string) => ({ editor, path, label, help })

  return (
    <div className="flex flex-col">
      <h2 className="font-semibold">{t('webSetCapabilities.memEmbeddingTitle')}</h2>
      <p className="text-small text-text-muted">{t('webSetCapabilities.memEmbeddingBody')}</p>
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
            type={{ kind: 'string' }}
          />
          <ConfigField
            {...field('memory.embedding.remote.model', t('webSetCapabilities.memModel'))}
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
            {...field('memory.embedding.remote.api_key_env', t('webSetCapabilities.memApiKeyEnv'))}
            type={{ kind: 'string' }}
          />
          <ConfigField
            {...field('memory.embedding.remote.dimensions', t('webSetCapabilities.memDimensions'))}
            type={{ kind: 'number' }}
          />
        </>
      ) : null}
      {provider === 'ollama' ? (
        <>
          <ConfigField
            {...field('memory.embedding.ollama.base_url', t('webSetCapabilities.memOllamaUrl'))}
            type={{ kind: 'string' }}
          />
          <ConfigField
            {...field('memory.embedding.ollama.model', t('webSetCapabilities.memModel'))}
            type={{ kind: 'string' }}
          />
        </>
      ) : null}
      {provider === 'local' ? (
        <ConfigField
          {...field('memory.embedding.local.model', t('webSetCapabilities.memLocalModel'))}
          type={{ kind: 'string' }}
        />
      ) : null}

      <h2 className="mt-4 font-semibold">{t('webSetCapabilities.memRetrievalTitle')}</h2>
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
      <SaveBar editor={editor} />
    </div>
  )
}
