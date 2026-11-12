import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Loader2, RefreshCw, Search, SearchX } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSetConfig'
import '@/i18n/en/webSettings'
import { buildApplyPayload, objToYaml } from '@/views/config/logic'
import { cn } from '@/lib/utils'
import { Button } from '@/web/components/ui/button'
import { fieldVariants, Input } from '@/web/components/ui/input'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/web/components/ui/tabs'
import { afterConfigWrite, useConfigEditor, type ConfigEditor } from '../config-editor'
import { isRevisionConflict } from '../config-errors'
import { ConfigField, useRegisterFields } from '../fields'
import { SaveBar, WriteBlockedNotice } from '../SaveBar'
import { Notice } from '../ui'
import { fieldsFromSchema, type JsonSchema } from '../schema'

/** Consecutive fields grouped by their top-level key ('' for top-level values). */
function groupByRoot<F extends { path: string }>(fields: F[]): [string, F[]][] {
  const groups: [string, F[]][] = []
  for (const field of fields) {
    const dot = field.path.indexOf('.')
    const root = dot < 0 ? '' : field.path.slice(0, dot)
    const last = groups.at(-1)
    if (last && last[0] === root) last[1].push(field)
    else groups.push([root, [field]])
  }
  return groups
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** An in-progress YAML edit and the configuration it started from. */
export interface YamlDraft {
  text: string
  baseline: string
  revision: string | null
}

function YamlEditor({
  baseline,
  revision,
  draft,
  onDraft,
  editor,
}: {
  baseline: string
  revision: string | null
  draft: YamlDraft | null
  onDraft(draft: YamlDraft | null): void
  editor: ConfigEditor
}) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const text = draft?.text ?? baseline
  // The draft outlives a refetch: a change elsewhere is announced, not
  // applied over the text being edited.
  const changed = draft !== null && draft.baseline !== baseline
  const formPending = editor.changes.length > 0 || editor.invalid.length > 0
  const apply = async () => {
    if (!draft) return
    setBusy(true)
    setError(null)
    try {
      const result = await rpc.call<{ restartRequired?: boolean }>('config.apply', {
        ...buildApplyPayload(draft.text, draft.baseline),
        ...(draft.revision ? { expectedRevision: draft.revision } : {}),
      })
      onDraft(null)
      await afterConfigWrite(queryClient, result)
    } catch (e) {
      setError(isRevisionConflict(e) ? t('webSetConfig.yamlConflict') : errorMessage(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="flex flex-col gap-3">
      <p className="text-small text-text-muted">{t('webSetConfig.advancedYamlHelp')}</p>
      {changed ? (
        <Notice
          role="status"
          tone="warn"
          actions={
            <Button size="sm" variant="outline" onClick={() => onDraft(null)}>
              {t('webSetConfig.yamlLoadNew')}
            </Button>
          }
        >
          {t('webSetConfig.yamlChanged')}
        </Notice>
      ) : null}
      <textarea
        aria-label={t('webSetConfig.advancedYaml')}
        value={text}
        onChange={(e) =>
          onDraft({
            text: e.target.value,
            baseline: draft?.baseline ?? baseline,
            revision: draft ? draft.revision : revision,
          })
        }
        spellCheck={false}
        rows={24}
        disabled={editor.writeBlocked}
        className={cn(
          fieldVariants({ size: null }),
          'min-h-[28rem] p-4 font-mono text-small leading-relaxed [resize:vertical] [tab-size:2]',
        )}
      />
      {error ? (
        <Notice role="alert" tone="danger" className="text-small">
          {t('webSetConfig.applyFailed', { message: error })}
        </Notice>
      ) : null}
      {formPending ? (
        <p className="text-small text-text-muted">{t('webSetConfig.yamlFormPending')}</p>
      ) : null}
      <div className="flex flex-row-reverse flex-wrap gap-2">
        <Button
          variant="primary"
          size="sm"
          disabled={
            busy || !draft || draft.text === draft.baseline || formPending || editor.writeBlocked
          }
          onClick={() => void apply()}
        >
          {busy ? <Loader2 className="animate-spin" aria-hidden /> : <Check aria-hidden />}
          {busy ? t('webSetConfig.applying') : t('webSetConfig.apply')}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={busy || !draft}
          onClick={() => {
            setError(null)
            onDraft(null)
          }}
        >
          {t('webSettings.discard')}
        </Button>
      </div>
    </div>
  )
}

/** Settings › Advanced: the whole configuration, form (from config.schema) or YAML. */
export function AdvancedSection() {
  const rpc = useRpc()
  const editor = useConfigEditor()
  const [filter, setFilter] = useState('')
  const [yamlDraft, setYamlDraft] = useState<YamlDraft | null>(null)
  const [tab, setTab] = useState('form')
  const schema = useQuery({
    queryKey: ['config.schema'],
    queryFn: async () => (await rpc.call<{ schema?: JsonSchema }>('config.schema'))?.schema ?? null,
    staleTime: Infinity,
    retry: false,
  })
  const fields = useMemo(
    () => (editor.ready ? fieldsFromSchema(editor.config, schema.data ?? null) : []),
    [editor.ready, editor.config, schema.data],
  )
  useRegisterFields(
    'advanced',
    fields.map((f) => ({
      id: f.path,
      label: f.title ? `${f.title} (${f.path})` : f.path,
    })),
  )
  const q = filter.trim().toLowerCase()
  const shown = q
    ? fields.filter((f) => `${f.path} ${f.title ?? ''} ${f.help ?? ''}`.toLowerCase().includes(q))
    : fields
  const baseline = useMemo(() => objToYaml(editor.config), [editor.config])
  const yamlDirty = yamlDraft !== null && yamlDraft.text !== yamlDraft.baseline
  const formDirty = editor.changes.length > 0 || editor.invalid.length > 0

  if (!editor.ready || schema.isPending) {
    return (
      <LoadingRegion className="flex flex-col gap-4">
        <Skeleton className="h-9 w-48" />
        <Skeleton className="h-9" />
        <Skeleton className="h-64 rounded-card" />
      </LoadingRegion>
    )
  }

  return (
    <div className="flex flex-col">
      {editor.writeBlocked ? <WriteBlockedNotice onReload={() => void editor.reload()} /> : null}
      <Tabs value={tab} onValueChange={setTab}>
        <div className="flex flex-wrap items-center gap-2">
          <TabsList>
            <TabsTrigger value="form">{t('webSetConfig.advancedForm')}</TabsTrigger>
            <TabsTrigger value="yaml">{t('webSetConfig.advancedYaml')}</TabsTrigger>
          </TabsList>
          <span className="flex-1" />
          <Button
            size="sm"
            variant="outline"
            // Reloading would drop the edits; the save bar's Discard is the way.
            disabled={formDirty || yamlDirty}
            title={formDirty || yamlDirty ? t('webSetConfig.reloadPending') : undefined}
            onClick={() => void editor.reload()}
          >
            <RefreshCw aria-hidden />
            {t('webSettings.reload')}
          </Button>
        </div>
        {/* Both panes stay mounted: switching keeps JSON drafts and focus targets. */}
        <TabsContent value="form" forceMount hidden={tab !== 'form'} className="mt-3">
          {schema.isError ? (
            <ErrorState
              message={errorMessage(schema.error)}
              onRetry={() => void schema.refetch()}
            />
          ) : null}
          {yamlDirty ? (
            <Notice tone="info" className="mb-2 text-small">
              {t('webSetConfig.formYamlPending')}
            </Notice>
          ) : null}
          <div className="set-sticky-filter sticky top-0 z-[1] -mx-1 mb-3 bg-surface-0/85 px-1 py-2 backdrop-blur">
            <Input
              type="search"
              icon={<Search />}
              aria-label={t('webSetConfig.advancedSearch')}
              placeholder={t('webSetConfig.advancedSearch')}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </div>
          {shown.length === 0 ? (
            <EmptyState size="sm" icon={<SearchX />} title={t('webSetConfig.advancedEmpty')} />
          ) : null}
          <div className="flex flex-col gap-4">
            {groupByRoot(shown).map(([root, group]) => (
              <section
                key={root}
                aria-label={root || t('webSetConfig.advancedGroupTop')}
                className="set-card py-1"
              >
                <h3 className="set-row-label border-b border-border py-2.5 font-mono text-small text-text-muted">
                  {root || t('webSetConfig.advancedGroupTop')}
                </h3>
                {group.map((field) => (
                  <ConfigField
                    key={field.path}
                    editor={editor}
                    path={field.path}
                    label={field.label}
                    detail={field.title}
                    help={field.help}
                    type={field.type}
                    optional={field.optional}
                    disabled={editor.writeBlocked || yamlDirty}
                  />
                ))}
              </section>
            ))}
          </div>
        </TabsContent>
        <TabsContent value="yaml" forceMount hidden={tab !== 'yaml'} className="mt-3">
          <YamlEditor
            baseline={baseline}
            revision={editor.revision}
            draft={yamlDraft}
            onDraft={setYamlDraft}
            editor={editor}
          />
        </TabsContent>
      </Tabs>
      {/* Outside the tabs: the leave guard holds whichever pane is showing. */}
      <SaveBar
        editor={editor}
        other={yamlDirty ? { onDiscard: () => setYamlDraft(null) } : undefined}
      />
    </div>
  )
}
