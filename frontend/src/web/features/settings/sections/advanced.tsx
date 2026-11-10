import { useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, TriangleAlert } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSetConfig'
import '@/i18n/en/webSettings'
import { buildApplyPayload, objToYaml } from '@/views/config/logic'
import { Button } from '@/web/components/ui/button'
import { ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/web/components/ui/tabs'
import { afterConfigWrite, useConfigEditor, type ConfigEditor } from '../config-editor'
import { isRevisionConflict } from '../config-errors'
import { ConfigField, useRegisterFields } from '../fields'
import { SaveBar, WriteBlockedNotice } from '../SaveBar'
import { fieldsFromSchema, type JsonSchema } from '../schema'

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
    <div className="flex flex-col gap-2">
      <p className="text-small text-text-muted">{t('webSetConfig.advancedYamlHelp')}</p>
      {changed ? (
        <div
          role="status"
          className="flex flex-wrap items-center gap-2 rounded-md border border-warn/50 bg-warn/10 px-3 py-2"
        >
          <TriangleAlert className="size-4 text-warn" aria-hidden data-status-icon />
          <span className="min-w-0 flex-1">{t('webSetConfig.yamlChanged')}</span>
          <Button size="sm" variant="outline" onClick={() => onDraft(null)}>
            {t('webSetConfig.yamlLoadNew')}
          </Button>
        </div>
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
        className="w-full rounded-md border border-border-control bg-code-bg p-3 font-mono text-small"
      />
      {error ? (
        <p role="alert" className="text-small text-danger">
          {t('webSetConfig.applyFailed', { message: error })}
        </p>
      ) : null}
      {formPending ? (
        <p className="text-small text-text-muted">{t('webSetConfig.yamlFormPending')}</p>
      ) : null}
      <div className="flex gap-2">
        <Button
          variant="primary"
          size="sm"
          disabled={
            busy || !draft || draft.text === draft.baseline || formPending || editor.writeBlocked
          }
          onClick={() => void apply()}
        >
          {busy ? t('webSetConfig.applying') : t('webSetConfig.apply')}
        </Button>
        <Button
          variant="outline"
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
      <LoadingRegion>
        <Skeleton className="h-64" />
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
            <p className="text-small text-text-muted">{t('webSetConfig.formYamlPending')}</p>
          ) : null}
          <input
            type="search"
            aria-label={t('webSetConfig.advancedSearch')}
            placeholder={t('webSetConfig.advancedSearch')}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="h-9 w-full max-w-xl rounded-md border border-border-control bg-surface-0 px-3"
          />
          {shown.length === 0 ? (
            <p className="py-4 text-text-muted">{t('webSetConfig.advancedEmpty')}</p>
          ) : null}
          <div className="divide-y divide-border">
            {shown.map((field) => (
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
