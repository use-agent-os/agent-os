import { useQuery } from '@tanstack/react-query'
import { BookOpen, FileText, FolderInput, NotebookPen, Trash2, Upload } from 'lucide-react'
import { useId, useRef, useState, type DragEvent } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { formatBytes, type MemoryFileRow } from '@/views/memory/logic'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { Card, Disclosure, Empty, Notice, Row } from '../parts'
import {
  MEMORY_KEY,
  errorText,
  useConnected,
  useDocuments,
  useMemoryAction,
  useRefreshWhenShown,
  useSourceFiles,
} from './api'
import {
  AGENT_ID,
  TEXT_EXTENSIONS,
  documentName,
  isOtherSource,
  replacedByPath,
  replacedByUpload,
  uploadVerdict,
  type UploadVerdict,
} from './logic'
import { refocus, rowButtonAt } from './focus'
import { Confirm, Sheet, Spinner, counted, fill } from './ui'

interface IngestResult {
  path?: string
  status?: string
  error?: string | null
  chunksIndexed?: number
}

const VERDICT_KEY: Record<Exclude<UploadVerdict, 'ok'>, Parameters<typeof fill>[0]> = {
  binary: 'settings.memory.docs.binary',
  unsupported: 'settings.memory.docs.unsupported',
  tooBig: 'settings.memory.docs.tooBig',
}

function formatDate(iso?: string): string {
  if (!iso) return t('settings.memory.dash')
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? t('settings.memory.dash')
    : d.toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
}

function fileMeta(file: MemoryFileRow): string {
  return fill('settings.memory.docs.meta', {
    lines: counted(file.lineCount ?? 0, 'line'),
    size: formatBytes(file.sizeBytes),
    date: formatDate(file.modifiedAt),
  })
}

/** Documents the agent can look things up in, and the notes it keeps itself. */
export function KnowledgeTab({ active }: { active: boolean }) {
  const [viewing, setViewing] = useState<string | null>(null)
  // The agent writes its own notes in the background: read both lists again on return.
  useRefreshWhenShown(active, [
    [...MEMORY_KEY, 'documents'],
    [...MEMORY_KEY, 'files'],
  ])
  return (
    <>
      <DocumentsCard onView={setViewing} />
      <SourcesCard onView={setViewing} />
      {viewing !== null ? <Viewer path={viewing} onClose={() => setViewing(null)} /> : null}
    </>
  )
}

function DocumentsCard({ onView }: { onView: (path: string) => void }) {
  const connected = useConnected()
  const docs = useDocuments()
  const action = useMemoryAction()
  const input = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [path, setPath] = useState('')
  const pathId = useId()
  const box = useRef<HTMLDivElement>(null)
  const busy = uploading !== null || action.busy !== null
  const disabled = !connected || busy
  const list = docs.data ?? []

  /** A document's row (its name, which opens it), by path. */
  function docButton(docPath: string): HTMLElement | null {
    const name = documentName(docPath)
    const row = Array.from(
      box.current?.querySelectorAll<HTMLElement>('li.mem-file-row') ?? [],
    ).find((li) => li.querySelector('.mem-file-row__name')?.textContent === name)
    return row?.querySelector<HTMLElement>('button') ?? null
  }

  /** Choose files: the header one, or the empty state's. */
  function chooseButton(): HTMLElement | null {
    const card = box.current?.closest('section')
    return (
      card?.querySelector<HTMLElement>('.stg-card__head button') ??
      box.current?.querySelector<HTMLElement>('.stg-empty button') ??
      null
    )
  }

  /** Files waiting for a yes before they replace documents of the same name. */
  const [replacing, setReplacing] = useState<
    | { kind: 'upload'; files: File[]; names: string[] }
    | { kind: 'path'; path: string; replaces: { kind: 'file' | 'folder'; name: string } }
    | null
  >(null)

  /** Check each file, then ask before any of them replaces a document. */
  function chooseFiles(files: File[]) {
    if (files.length === 0 || !connected) return
    const ok: File[] = []
    for (const file of files) {
      const verdict = uploadVerdict(file.name, file.size)
      if (verdict === 'ok') ok.push(file)
      else toast.error(fill(VERDICT_KEY[verdict], { name: file.name }))
    }
    if (input.current) input.current.value = ''
    if (ok.length === 0) return
    const names = replacedByUpload(
      ok.map((f) => f.name),
      list,
    )
    if (names.length > 0) setReplacing({ kind: 'upload', files: ok, names })
    else void upload(ok, new Set())
  }

  async function upload(files: File[], replaced: ReadonlySet<string>) {
    let added = 0
    let swapped = 0
    let last: string | null = null
    for (const file of files) {
      setUploading(file.name)
      const res = await action.run('ingest', async () =>
        action.rpc.call<{ results?: IngestResult[] }>('memory.knowledge_base.ingest', {
          agentId: AGENT_ID,
          filename: file.name,
          content: await file.text(),
        }),
      )
      const failed = res?.results?.find((r) => r.status === 'error')
      if (failed) {
        toast.error(
          fill('settings.memory.docs.failed', { name: file.name, error: failed.error ?? '' }),
        )
      } else if (res) {
        if (replaced.has(file.name)) swapped += 1
        else added += 1
        last = file.name
      }
    }
    setUploading(null)
    const done = addedToast(added, swapped)
    if (done) toast.success(done, { id: 'stg-memory-docs' })
    // Choose files was greyed out while adding: land on what was added.
    refocus(
      box.current,
      () => (last ? docButton(`knowledge_base/${last}`) : null) ?? chooseButton(),
    )
  }

  function startPath() {
    const target = path.trim()
    if (!target) return
    const replaces = replacedByPath(target, list)
    if (replaces) setReplacing({ kind: 'path', path: target, replaces })
    else void addPath(target, false)
  }

  async function addPath(target: string, replaces: boolean) {
    const res = await action.run('path', () =>
      action.rpc.call<{ results?: IngestResult[] }>('memory.knowledge_base.ingest', {
        agentId: AGENT_ID,
        path: target,
      }),
    )
    if (!res) return
    const results = res.results ?? []
    const failed = results.filter((r) => r.status === 'error')
    const ok = results.length - failed.length
    for (const r of failed.slice(0, 3)) {
      toast.error(
        fill('settings.memory.docs.failed', {
          name: documentName(r.path ?? target),
          error: r.error ?? '',
        }),
      )
    }
    if (ok > 0) {
      setPath('')
      const first = results.find((r) => r.status !== 'error')?.path
      refocus(
        box.current,
        () => (first ? docButton(first) : null) ?? document.getElementById(pathId),
      )
      // One file over one document is a replacement; a folder merge is told as added.
      const single = replaces && ok === 1 && results.length === 1
      toast.success(addedToast(single ? 0 : ok, single ? 1 : 0)!, { id: 'stg-memory-docs' })
    } else if (results.length === 0) {
      toast.error(fill('settings.memory.docs.nothingFound', { path: target }))
    }
  }

  function confirmReplace() {
    const pending = replacing
    setReplacing(null)
    if (!pending) return
    if (pending.kind === 'upload') void upload(pending.files, new Set(pending.names))
    else void addPath(pending.path, pending.replaces.kind === 'file')
  }

  async function remove(doc: string) {
    const at = list.findIndex((d) => d.path === doc)
    const done = await action.run('remove', async () => {
      await action.rpc.call('memory.knowledge_base.remove', { agentId: AGENT_ID, path: doc })
      return true
    })
    if (done) {
      toast.success(fill('settings.memory.docs.removed', { name: documentName(doc) }), {
        id: 'stg-memory-docs',
      })
    }
    setRemoving(null)
    // Its row goes: land on the next document, else the one before, else Choose files.
    refocus(box.current, () =>
      done
        ? (rowButtonAt(box.current, 'li.mem-file-row', Math.max(0, at)) ?? chooseButton())
        : (docButton(doc) ?? chooseButton()),
    )
  }

  function onDrag(e: DragEvent) {
    if (!e.dataTransfer.types.includes('Files')) return
    e.preventDefault()
    e.dataTransfer.dropEffect = disabled ? 'none' : 'copy'
    if (e.type === 'dragenter' || e.type === 'dragover') setDragging(true)
  }

  function onDragLeave(e: DragEvent) {
    if (e.currentTarget.contains(e.relatedTarget as Node | null)) return
    setDragging(false)
  }

  function onDrop(e: DragEvent) {
    if (!e.dataTransfer.types.includes('Files')) return
    e.preventDefault()
    setDragging(false)
    if (!disabled) chooseFiles(Array.from(e.dataTransfer.files))
  }

  const choose = (
    <Button disabled={disabled} onClick={() => input.current?.click()}>
      <Upload className="size-3.5" strokeWidth={1.75} aria-hidden />
      {t('settings.memory.docs.choose')}
    </Button>
  )

  return (
    <div
      className="mem-drop"
      data-dragging={dragging ? 'true' : undefined}
      onDragEnter={onDrag}
      onDragOver={onDrag}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <Card
        title={t('settings.memory.docs.title')}
        blurb={t('settings.memory.docs.blurb')}
        icon={
          <span className="mem-card-icon" aria-hidden>
            <BookOpen />
          </span>
        }
        action={list.length > 0 ? choose : undefined}
      >
        <input
          ref={input}
          type="file"
          multiple
          hidden
          accept={TEXT_EXTENSIONS.join(',')}
          aria-label={t('settings.memory.docs.choose')}
          onChange={(e) => chooseFiles(Array.from(e.target.files ?? []))}
        />
        <div className="mem-docs" ref={box}>
          {!connected ? (
            <p className="mem-quiet">{t('settings.memory.offlineList')}</p>
          ) : docs.isPending ? (
            <Spinner />
          ) : docs.isError ? (
            <Notice
              tone="danger"
              action={
                <Button onClick={() => void docs.refetch()}>{t('settings.memory.retry')}</Button>
              }
            >
              {t('settings.memory.docs.loadFailed')} {errorText(docs.error)}
            </Notice>
          ) : list.length === 0 ? (
            <div className="mem-dropzone">
              <Empty
                icon={<Upload />}
                title={
                  dragging ? t('settings.memory.docs.dropActive') : t('settings.memory.docs.drop')
                }
                body={t('settings.memory.docs.empty.body')}
                action={
                  <Button
                    variant="primary"
                    disabled={disabled}
                    onClick={() => input.current?.click()}
                  >
                    <Upload className="size-3.5" strokeWidth={1.75} aria-hidden />
                    {t('settings.memory.docs.choose')}
                  </Button>
                }
              />
            </div>
          ) : (
            <>
              <ul className="mem-files" aria-label={t('settings.memory.docs.title')}>
                {list.map((doc) => (
                  <li key={doc.path} className="mem-file-row">
                    <span className="mem-file-row__icon" aria-hidden>
                      <FileText />
                    </span>
                    <button
                      type="button"
                      className="mem-file-row__main app-no-drag"
                      title={doc.path}
                      onClick={() => onView(doc.path)}
                    >
                      <span className="mem-file-row__name">{documentName(doc.path)}</span>
                      <span className="mem-file-row__meta">{fileMeta(doc)}</span>
                    </button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="mem-danger-icon"
                      disabled={disabled}
                      aria-label={fill('settings.memory.docs.remove', {
                        name: documentName(doc.path),
                      })}
                      title={t('settings.memory.remove')}
                      onClick={() => setRemoving(doc.path)}
                    >
                      <Trash2 className="size-3.5" strokeWidth={1.75} aria-hidden />
                    </Button>
                  </li>
                ))}
              </ul>
              <p className="mem-drop-hint" aria-hidden>
                {dragging
                  ? t('settings.memory.docs.dropActive')
                  : t('settings.memory.docs.dropHint')}
              </p>
            </>
          )}
          {uploading ? (
            <Spinner label={fill('settings.memory.docs.uploading', { name: uploading })} />
          ) : null}
        </div>
        <Disclosure
          title={t('settings.memory.docs.path.title')}
          hint={t('settings.memory.docs.path.hint')}
        >
          <Row
            label={t('settings.memory.docs.path.label')}
            help={t('settings.memory.docs.path.help')}
            htmlFor={pathId}
            align="start"
          >
            <span className="mem-inline">
              <input
                id={pathId}
                className="mac-input"
                data-mono="true"
                value={path}
                placeholder="docs/handbook.pdf"
                autoComplete="off"
                spellCheck={false}
                disabled={!connected || busy}
                onChange={(e) => setPath(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    startPath()
                  }
                }}
              />
              <Button disabled={!connected || busy || !path.trim()} onClick={startPath}>
                <FolderInput className="size-3.5" strokeWidth={1.75} aria-hidden />
                {action.busy === 'path'
                  ? t('settings.memory.docs.path.adding')
                  : t('settings.memory.docs.path.add')}
              </Button>
            </span>
          </Row>
        </Disclosure>
      </Card>

      {replacing !== null ? (
        <ConfirmReplace
          pending={replacing}
          onCancel={() => setReplacing(null)}
          onConfirm={confirmReplace}
        />
      ) : null}
      {removing !== null ? (
        <ConfirmRemoveDoc
          path={removing}
          busy={action.busy === 'remove'}
          onCancel={() => setRemoving(null)}
          onConfirm={() => void remove(removing)}
        />
      ) : null}
    </div>
  )
}

/** "Document added.", "Document replaced.", "2 documents added and 1 replaced.", or null. */
function addedToast(added: number, replaced: number): string | null {
  if (added > 0 && replaced > 0) {
    return fill('settings.memory.docs.addedReplaced', {
      added: counted(added, 'doc'),
      replaced: replaced.toLocaleString('en-US'),
    })
  }
  if (replaced === 1) return t('settings.memory.docs.replaced.one')
  if (replaced > 1) return fill('settings.memory.docs.replaced.many', { count: replaced })
  if (added === 1) return t('settings.memory.docs.added.one')
  if (added > 1) return fill('settings.memory.docs.added.many', { count: added })
  return null
}

/** Asks before an upload or a workspace path overwrites what is already there. */
function ConfirmReplace({
  pending,
  onCancel,
  onConfirm,
}: {
  pending:
    | { kind: 'upload'; files: File[]; names: string[] }
    | { kind: 'path'; path: string; replaces: { kind: 'file' | 'folder'; name: string } }
  onCancel: () => void
  onConfirm: () => void
}) {
  if (pending.kind === 'path' && pending.replaces.kind === 'folder') {
    return (
      <Confirm
        title={fill('settings.memory.docs.replace.folder.title', { name: pending.replaces.name })}
        body={fill('settings.memory.docs.replace.folder.body', { name: pending.replaces.name })}
        confirm={t('settings.memory.docs.replace.folder.confirm')}
        onCancel={onCancel}
        onConfirm={onConfirm}
      />
    )
  }
  const names = pending.kind === 'upload' ? pending.names : [pending.replaces.name]
  const others = pending.kind === 'upload' ? pending.files.length - names.length : 0
  const one = names.length === 1
  return (
    <Confirm
      title={
        one
          ? fill('settings.memory.docs.replace.title.one', { name: names[0]! })
          : fill('settings.memory.docs.replace.title.many', { count: names.length })
      }
      body={
        <>
          <p>
            {one
              ? fill('settings.memory.docs.replace.body.one', { name: names[0]! })
              : t('settings.memory.docs.replace.body.many')}
          </p>
          {one ? null : (
            <ul className="mem-confirm__list">
              {names.map((name) => (
                <li key={name}>{name}</li>
              ))}
            </ul>
          )}
          {others > 0 ? (
            <p>
              {others === 1
                ? t('settings.memory.docs.replace.alsoNew.one')
                : fill('settings.memory.docs.replace.alsoNew', { files: counted(others, 'file') })}
            </p>
          ) : null}
        </>
      }
      confirm={t('settings.memory.docs.replace.confirm')}
      onCancel={onCancel}
      onConfirm={onConfirm}
    />
  )
}

function ConfirmRemoveDoc({
  path,
  busy,
  onCancel,
  onConfirm,
}: {
  path: string
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <Confirm
      title={fill('settings.memory.docs.confirmRemove.title', { name: documentName(path) })}
      body={t('settings.memory.docs.confirmRemove.body')}
      confirm={t('settings.memory.confirmRemove.confirm')}
      busy={busy}
      onCancel={onCancel}
      onConfirm={onConfirm}
    />
  )
}

function SourcesCard({ onView }: { onView: (path: string) => void }) {
  const connected = useConnected()
  const files = useSourceFiles()
  const notes = (files.data ?? []).filter(isOtherSource)
  return (
    <Card
      title={t('settings.memory.sources.title')}
      blurb={t('settings.memory.sources.blurb')}
      icon={
        <span className="mem-card-icon" aria-hidden>
          <NotebookPen />
        </span>
      }
    >
      <div className="mem-docs">
        {!connected ? (
          <p className="mem-quiet">{t('settings.memory.offlineList')}</p>
        ) : files.isPending ? (
          <Spinner />
        ) : files.isError ? (
          <Notice
            tone="danger"
            action={
              <Button onClick={() => void files.refetch()}>{t('settings.memory.retry')}</Button>
            }
          >
            {t('settings.memory.sources.failed')} {errorText(files.error)}
          </Notice>
        ) : notes.length === 0 ? (
          <p className="mem-quiet">{t('settings.memory.sources.empty')}</p>
        ) : (
          <ul className="mem-files" aria-label={t('settings.memory.sources.title')}>
            {notes.map((file) => (
              <li key={file.path} className="mem-file-row">
                <span className="mem-file-row__icon" aria-hidden>
                  <FileText />
                </span>
                <div className="mem-file-row__main" title={file.path}>
                  <span className="mem-file-row__name">{file.path}</span>
                  <span className="mem-file-row__meta">{fileMeta(file)}</span>
                </div>
                <Button
                  aria-label={fill('settings.memory.viewNamed', { name: file.path })}
                  onClick={() => onView(file.path)}
                >
                  {t('settings.memory.view')}
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  )
}

interface ShowResult {
  content?: string
  truncated?: boolean
  lineCount?: number
}

/** Read-only view of one indexed file, as the agent sees it. */
function Viewer({ path, onClose }: { path: string; onClose: () => void }) {
  const rpc = useRpc()
  const content = useQuery({
    queryKey: ['settings', 'memory', 'show', path],
    queryFn: () =>
      // A line slice never trips the gateway's whole-file size refusal.
      rpc.call<ShowResult>('memory.show', { agentId: AGENT_ID, path, fromLine: 1, lines: 500 }),
  })
  return (
    <Sheet
      wide
      title={<span className="mem-sheet__path">{documentName(path)}</span>}
      blurb={t('settings.memory.viewer.blurb')}
      onClose={onClose}
      foot={
        content.data?.truncated ? (
          <span className="mem-quiet">{t('settings.memory.viewer.truncated')}</span>
        ) : undefined
      }
    >
      {content.isPending ? (
        <Spinner />
      ) : content.isError ? (
        <Notice tone="danger">
          {t('settings.memory.viewer.failed')} {errorText(content.error)}
        </Notice>
      ) : content.data?.content ? (
        <pre className="mem-pre" tabIndex={0}>
          {content.data.content}
        </pre>
      ) : (
        <p className="mem-quiet">{t('settings.memory.viewer.empty')}</p>
      )}
    </Sheet>
  )
}
