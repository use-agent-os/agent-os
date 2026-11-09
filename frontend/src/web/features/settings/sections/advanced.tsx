import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSetConfig'
import { buildApplyPayload, objToYaml } from '@/views/config/logic'
import { Button } from '@/web/components/ui/button'
import { ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/web/components/ui/tabs'
import { CONFIG_SNAPSHOT_KEY, useConfigEditor, useRestartNotice } from '../config-editor'
import { ConfigField, useRegisterFields } from '../fields'
import { SaveBar } from '../SaveBar'
import { fieldsFromSchema, type JsonSchema } from '../schema'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function YamlEditor({ baseline, revision }: { baseline: string; revision?: string | null }) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState(baseline)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const apply = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await rpc.call<{ restartRequired?: boolean; restartReasons?: string[] }>(
        'config.apply',
        {
          ...buildApplyPayload(draft, baseline),
          ...(revision ? { expectedRevision: revision } : {}),
        },
      )
      if (result?.restartRequired) useRestartNotice.getState().show(result.restartReasons ?? [])
      await queryClient.invalidateQueries({ queryKey: CONFIG_SNAPSHOT_KEY })
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="flex flex-col gap-2">
      <p className="text-small text-text-muted">{t('webSetConfig.advancedYamlHelp')}</p>
      <textarea
        aria-label={t('webSetConfig.advancedYaml')}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        spellCheck={false}
        rows={24}
        className="w-full rounded-md border border-border-control bg-code-bg p-3 font-mono text-small"
      />
      {error ? (
        <p role="alert" className="text-small text-danger">
          {t('webSetConfig.applyFailed', { message: error })}
        </p>
      ) : null}
      <div>
        <Button
          variant="primary"
          size="sm"
          disabled={busy || draft === baseline}
          onClick={() => void apply()}
        >
          {busy ? t('webSetConfig.applying') : t('webSetConfig.apply')}
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
      label: f.label,
    })),
  )
  const q = filter.trim().toLowerCase()
  const shown = q
    ? fields.filter((f) => `${f.path} ${f.label} ${f.help ?? ''}`.toLowerCase().includes(q))
    : fields
  const baseline = useMemo(() => objToYaml(editor.config), [editor.config])

  if (!editor.ready) {
    return (
      <LoadingRegion>
        <Skeleton className="h-64" />
      </LoadingRegion>
    )
  }
  if (schema.isPending) {
    return (
      <LoadingRegion>
        <Skeleton className="h-64" />
      </LoadingRegion>
    )
  }

  return (
    <Tabs defaultValue="form">
      <TabsList>
        <TabsTrigger value="form">{t('webSetConfig.advancedForm')}</TabsTrigger>
        <TabsTrigger value="yaml">{t('webSetConfig.advancedYaml')}</TabsTrigger>
      </TabsList>
      <TabsContent value="form" className="mt-3">
        {schema.isError ? (
          <ErrorState message={errorMessage(schema.error)} onRetry={() => void schema.refetch()} />
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
              help={field.help}
              type={field.type}
              disabled={editor.writeBlocked}
            />
          ))}
        </div>
        <SaveBar editor={editor} />
      </TabsContent>
      <TabsContent value="yaml" className="mt-3">
        <YamlEditor key={baseline} baseline={baseline} revision={editor.revision} />
      </TabsContent>
    </Tabs>
  )
}
