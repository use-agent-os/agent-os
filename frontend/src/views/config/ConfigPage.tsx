import './config.css'
import { useCallback, useEffect, useId, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CheckIcon,
  EyeIcon,
  EyeOffIcon,
  HelpCircleIcon,
  RefreshCwIcon,
  SearchIcon,
  SlidersHorizontalIcon,
} from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { useRpc } from '@/app/providers'
import {
  isMethodNotFoundRpcError,
  SETTINGS_SNAPSHOT_QUERY_KEY,
  type SettingsSnapshot,
} from '@/views/settings/snapshot'
import {
  buildApplyPayload,
  buildPatchPayload,
  computeDirty,
  dirtyCount,
  entriesForTab,
  fieldKind,
  fieldLabel,
  groupEntries,
  hasInvalidJson,
  helpFor,
  isSensitiveKey,
  objectSummary,
  objToYaml,
  parseFieldValue,
  summariseDiffValue,
  TABS,
  type ConfigData,
  type DirtyMap,
  type InvalidJsonMap,
  type TabDef,
} from './logic'

// config.js:722-745 — the config.patch / config.apply responses carry an
// optional restartRequired advisory flag.
interface SaveResult {
  restartRequired?: boolean
}

interface ConfigEditorSnapshot {
  config: ConfigData
  revision?: string
  diskDiverged?: boolean
  writeBlocked?: boolean
}

interface ConfigPageProps {
  embedded?: boolean
  /**
   * Embedded Settings owns the authoritative snapshot. `null` means that
   * snapshot is still loading; `undefined` keeps the standalone loader.
   */
  externalSnapshot?: SettingsSnapshot | null
  externalSnapshotError?: boolean
  onSnapshotReload?: () => Promise<SettingsSnapshot | undefined>
}

type EditorMode = 'form' | 'yaml'

const SEARCH_TAB: TabDef = { id: 'search', label: 'Search results', prefixes: [] }

// ── Per-field help tooltip trigger ───────────────────────────────────────────
function HelpTrigger({ configKey }: { configKey: string }) {
  return (
    <span className="cfg-help">
      <button type="button" className="cfg-help__btn" aria-label={`Help for ${configKey}`}>
        <HelpCircleIcon aria-hidden="true" />
      </button>
      {/* config.js:747-810 — help copy, surfaced on hover/focus (CSS-driven, no
          hand-positioned floating layer). Rendered inline so it is queryable. */}
      <span className="cfg-help__tip" role="tooltip">
        {helpFor(configKey)}
      </span>
    </span>
  )
}

// ── A single editable leaf field ─────────────────────────────────────────────
function ConfigField({
  configKey,
  value,
  groupId,
  dirty,
  invalid,
  jsonDraft,
  onChange,
}: {
  configKey: string
  value: unknown
  groupId: string
  dirty: boolean
  invalid: boolean
  // config.js:545 — in-progress raw object-field text (valid or invalid) to seed
  // the uncontrolled textarea on mount, so it survives an unmount/remount; falls
  // back to the canonical serialisation when there is no draft.
  jsonDraft: string | undefined
  onChange: (key: string, kind: 'boolean' | 'number' | 'json' | 'string', raw: string) => void
}) {
  const inputId = useId()
  const [secretVisible, setSecretVisible] = useState(false)
  const label = fieldLabel(configKey, groupId)
  const kind = fieldKind(configKey, value)

  const labelRow = (
    <div className="cfg-field__label-row">
      <label className="t-label cfg-field__label" htmlFor={inputId} title={configKey}>
        {label}
      </label>
      <HelpTrigger configKey={configKey} />
    </div>
  )

  let control: React.ReactNode
  if (kind === 'readonly') {
    // config.js:531-535 — no data-cfg-key: save/dirty tracking never sees it.
    control = (
      <div className="cfg-field__control">
        <span id={inputId} className="cfg-readonly" data-cfg-readonly={configKey}>
          {String(value ?? '')}
        </span>
      </div>
    )
  } else if (kind === 'boolean') {
    const checked = Boolean(value)
    control = (
      <label className="cfg-switch">
        <input
          id={inputId}
          type="checkbox"
          aria-label={configKey}
          checked={checked}
          onChange={(e) => onChange(configKey, 'boolean', e.target.checked ? 'true' : '')}
        />
        <span className="cfg-switch__track" aria-hidden="true">
          <span className="cfg-switch__thumb" />
        </span>
        <span className="cfg-switch__text">{checked ? 'Enabled' : 'Disabled'}</span>
      </label>
    )
  } else if (kind === 'number') {
    control = (
      <input
        id={inputId}
        className="cfg-input cfg-input--number"
        type="number"
        value={String(value ?? '')}
        onChange={(e) => onChange(configKey, 'number', e.target.value)}
      />
    )
  } else if (kind === 'object') {
    // config.js:545-546 — seed from the in-progress draft when present (so an
    // invalid draft is not silently reverted to the canonical serialisation on
    // remount), else the pretty-printed canonical value.
    const jsonStr = jsonDraft !== undefined ? jsonDraft : JSON.stringify(value, null, 2)
    const errorId = `${inputId}-error`
    control = (
      <details className="cfg-object" open={dirty || invalid}>
        <summary>
          <span className="cfg-object__summary">{objectSummary(value)}</span>
          <span className="cfg-object__action">Edit</span>
        </summary>
        <textarea
          id={inputId}
          className="cfg-input cfg-input--json"
          aria-label={configKey}
          aria-describedby={errorId}
          defaultValue={jsonStr}
          rows={Math.min(Math.max(jsonStr.split('\n').length + 1, 4), 12)}
          onChange={(e) => onChange(configKey, 'json', e.target.value)}
        />
        <div id={errorId} className={`cfg-json-error${invalid ? '' : ' cfg-hidden'}`}>
          Invalid JSON
        </div>
      </details>
    )
  } else {
    const sensitive = isSensitiveKey(configKey)
    control = (
      <div className="cfg-field__control">
        <input
          id={inputId}
          className="cfg-input cfg-input--text"
          type={sensitive && !secretVisible ? 'password' : 'text'}
          value={String(value ?? '')}
          onChange={(e) => onChange(configKey, 'string', e.target.value)}
        />
        {sensitive ? (
          <button
            type="button"
            className="cfg-secret-toggle"
            aria-label={`${secretVisible ? 'Hide' : 'Show'} ${configKey}`}
            aria-pressed={secretVisible}
            onClick={() => setSecretVisible((visible) => !visible)}
          >
            {secretVisible ? <EyeOffIcon aria-hidden="true" /> : <EyeIcon aria-hidden="true" />}
          </button>
        ) : null}
      </div>
    )
  }

  const classes = [
    'cfg-field',
    kind === 'object' ? 'cfg-field--object' : '',
    dirty ? 'cfg-field--dirty' : '',
    invalid ? 'cfg-field--invalid' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div className={classes}>
      {labelRow}
      {control}
    </div>
  )
}

// ── One form tab's grouped fields ────────────────────────────────────────────
function FormTab({
  config,
  tab,
  search,
  editedValues,
  dirty,
  invalid,
  jsonDrafts,
  resetKey,
  onChange,
}: {
  config: ConfigData
  tab: TabDef
  search: string
  editedValues: Record<string, unknown>
  dirty: DirtyMap
  invalid: InvalidJsonMap
  jsonDrafts: Record<string, string>
  // Bumped on every fresh config snapshot (load / reload / discard / post-save)
  // so uncontrolled object-field textareas remount and drop stale draft text —
  // config.js:302 re-rendered the panel HTML wholesale on each _loadData.
  resetKey: number
  onChange: (key: string, kind: 'boolean' | 'number' | 'json' | 'string', raw: string) => void
}) {
  const entries = entriesForTab(config, tab, search)
  if (entries.length === 0) {
    return <div className="cfg-empty">No matching fields</div>
  }
  const groups = groupEntries(entries)
  return (
    <>
      {groups.map((group) => (
        <section className="cfg-group" key={group.id} aria-label={group.title}>
          <header className="cfg-group__head">
            <h2 className="cfg-group__title t-label">{group.title}</h2>
            <span className="cfg-group__meta t-data">
              {group.entries.length} {group.entries.length === 1 ? 'field' : 'fields'}
            </span>
          </header>
          <div className="cfg-group__fields">
            {group.entries.map(([key, loadedValue]) => {
              // The displayed value is the pending edit if present, else loaded.
              const value = key in editedValues ? editedValues[key] : loadedValue
              return (
                <ConfigField
                  key={`${resetKey}:${key}`}
                  configKey={key}
                  value={value}
                  groupId={group.id}
                  dirty={key in dirty}
                  invalid={key in invalid}
                  jsonDraft={key in jsonDrafts ? jsonDrafts[key] : undefined}
                  onChange={onChange}
                />
              )
            })}
          </div>
        </section>
      ))}
    </>
  )
}

// ── Sticky save bar (shown only when dirty) ──────────────────────────────────
function StickyBar({
  count,
  yamlMode,
  dirty,
  invalid,
  diffOpen,
  onToggleDiff,
  onDiscard,
  onSave,
  saving,
  saveDisabled,
}: {
  count: number
  yamlMode: boolean
  dirty: DirtyMap
  invalid: InvalidJsonMap
  diffOpen: boolean
  onToggleDiff: () => void
  onDiscard: () => void
  onSave: () => void
  saving: boolean
  saveDisabled: boolean
}) {
  const diffId = useId()
  return (
    <div
      className="cfg-stickybar panel tone-info tone-rail"
      role="region"
      aria-label="Pending changes"
      aria-live="polite"
    >
      <div className="cfg-stickybar__row">
        <span className="cfg-stickybar__pulse" aria-hidden="true" />
        <span className="cfg-stickybar__count">
          <strong>{count}</strong> <span>{count === 1 ? 'change pending' : 'changes pending'}</span>
        </span>
        <span className="cfg-stickybar__sep" aria-hidden="true">
          ·
        </span>
        <button
          type="button"
          className="cfg-stickybar__diff-toggle"
          aria-expanded={diffOpen}
          aria-controls={diffId}
          onClick={onToggleDiff}
        >
          {diffOpen ? 'Hide diff' : 'View diff'}
        </button>
        <span className="cfg-stickybar__spacer" />
        <Button type="button" variant="ghost" size="sm" disabled={saving} onClick={onDiscard}>
          Discard
        </Button>
        <Button type="button" size="sm" disabled={saveDisabled} onClick={onSave}>
          <CheckIcon />
          <span>Save</span>
        </Button>
      </div>
      {diffOpen ? (
        <div className="cfg-stickybar__diff" id={diffId}>
          {yamlMode ? (
            <div className="cfg-diff-row">
              <span className="cfg-diff-row__key t-data">YAML</span>
              <span className="cfg-diff-row__old">loaded config</span>
              <span className="cfg-diff-row__arrow" aria-hidden="true">
                {'->'}
              </span>
              <span className="cfg-diff-row__new">unsaved draft</span>
            </div>
          ) : (
            <>
              {Object.entries(dirty)
                .filter(([key]) => !(key in invalid))
                .map(([key, { old: oldV, new: newV }]) => (
                  <div className="cfg-diff-row" key={key}>
                    <span className="cfg-diff-row__key t-data">{key}</span>
                    <span className="cfg-diff-row__old">{summariseDiffValue(oldV)}</span>
                    <span className="cfg-diff-row__arrow" aria-hidden="true">
                      {'->'}
                    </span>
                    <span className="cfg-diff-row__new">{summariseDiffValue(newV)}</span>
                  </div>
                ))}
              {Object.keys(invalid).map((key) => (
                <div className="cfg-diff-row" key={key}>
                  <span className="cfg-diff-row__key t-data">{key}</span>
                  <span className="cfg-diff-row__old">loaded JSON</span>
                  <span className="cfg-diff-row__arrow" aria-hidden="true">
                    {'->'}
                  </span>
                  <span className="cfg-diff-row__new">Fix invalid JSON</span>
                </div>
              ))}
            </>
          )}
        </div>
      ) : null}
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────
export function ConfigPage({
  embedded = false,
  externalSnapshot,
  externalSnapshotError = false,
  onSnapshotReload,
}: ConfigPageProps = {}) {
  const rpc = useRpc()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const usesExternalSnapshot = externalSnapshot !== undefined

  const [mode, setMode] = useState<EditorMode>('form')
  const [activeTab, setActiveTab] = useState<string>('core')
  const [search, setSearch] = useState('')
  const [diffOpen, setDiffOpen] = useState(false)
  const [formResetKey, setFormResetKey] = useState(0)
  const [snapshotConflict, setSnapshotConflict] = useState(false)

  // Form-mode edit state (all derived against the loaded config snapshot).
  const [editedValues, setEditedValues] = useState<Record<string, unknown>>({})
  const [dirty, setDirty] = useState<DirtyMap>({})
  const [invalid, setInvalid] = useState<InvalidJsonMap>({})
  // config.js:185,545,593-609 — in-progress raw object-field JSON text, keyed by
  // dotted key. Preserved (including invalid drafts) so that unmounting a field
  // — e.g. switching tabs — and remounting it restores the exact text the user
  // typed, keeping it consistent with the still-set "Invalid JSON" flag. Cleared
  // only on a no-op revert and on any fresh snapshot / discard.
  const [jsonDrafts, setJsonDrafts] = useState<Record<string, string>>({})

  // YAML-mode draft state (baseline = objToYaml(loaded config)).
  const [yamlDraft, setYamlDraft] = useState<string | null>(null)

  useEffect(() => {
    if (!embedded) document.title = 'Config - AgentOS Control'
  }, [embedded])

  const configQuery = useQuery<ConfigEditorSnapshot>({
    queryKey: ['config', 'editor'],
    queryFn: async () => {
      await rpc.waitForConnection()
      try {
        const snapshot = await rpc.call<SettingsSnapshot>('config.snapshot')
        if (!snapshot || !Object.prototype.hasOwnProperty.call(snapshot, 'config')) {
          throw new Error('config.snapshot returned an invalid response')
        }
        return {
          config: (snapshot.config as ConfigData | undefined) ?? {},
          revision: snapshot.revision ?? undefined,
          diskDiverged: snapshot.diskDiverged,
          writeBlocked: snapshot.writeBlocked,
        }
      } catch (error) {
        if (!isMethodNotFoundRpcError(error)) throw error
      }
      // Compatibility fallback for an older gateway during a rolling upgrade.
      const data = await rpc.call<ConfigData>('config.get')
      return { config: data ?? {} }
    },
    enabled: !usesExternalSnapshot,
    refetchOnWindowFocus: false,
  })

  // config.js:305 — load-failure toast (stable id so repeats dedupe).
  useEffect(() => {
    if (!usesExternalSnapshot && configQuery.isError) {
      const err = configQuery.error
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to load config: ' + message, { id: 'config-load-err' })
    }
  }, [usesExternalSnapshot, configQuery.isError, configQuery.error])

  const editorSnapshot = usesExternalSnapshot
    ? externalSnapshot
      ? {
          config: (externalSnapshot.config as ConfigData | undefined) ?? {},
          revision: externalSnapshot.revision ?? undefined,
          diskDiverged: externalSnapshot.diskDiverged,
          writeBlocked: externalSnapshot.writeBlocked,
        }
      : undefined
    : configQuery.data
  const config = useMemo(() => editorSnapshot?.config ?? {}, [editorSnapshot?.config])
  const configRevision = editorSnapshot?.revision
  const diskDiverged = editorSnapshot?.diskDiverged === true
  const writeBlocked = editorSnapshot?.writeBlocked === true
  const baselineYaml = useMemo(() => objToYaml(config), [config])
  const yamlHasChanges = yamlDraft !== null && yamlDraft !== baselineYaml

  // Reset all edit state whenever a fresh config snapshot arrives (load / reload
  // / post-save). config.js:295-305,732,742 blanked _dirty/_invalidJson/_yamlDraft.
  // Done as a render-phase reset keyed on the query's dataUpdatedAt (React's
  // supported "adjust state when a prop/derived value changes" pattern) rather
  // than an effect, so the blank state is applied before paint with no cascading
  // second render.
  const snapshotIdentity = usesExternalSnapshot
    ? (externalSnapshot?.revision ?? externalSnapshot)
    : configQuery.dataUpdatedAt
  const [lastSnapshotIdentity, setLastSnapshotIdentity] = useState<unknown>(snapshotIdentity)
  const hasPendingDraft =
    Object.keys(dirty).length > 0 || Object.keys(invalid).length > 0 || yamlHasChanges
  if (snapshotIdentity && snapshotIdentity !== lastSnapshotIdentity) {
    setLastSnapshotIdentity(snapshotIdentity)
    // A background Settings refresh may carry an unrelated guided change. Keep
    // an operator's in-progress Advanced draft intact; explicit discard/save
    // paths below own the reset. Pristine editors safely adopt the new snapshot.
    if (hasPendingDraft) {
      // Never let an edit based on revision A silently inherit revision B's CAS
      // token. Keep the draft visible for comparison, but require an explicit
      // discard/reload before any write can proceed.
      setSnapshotConflict(true)
    } else {
      setEditedValues({})
      setDirty({})
      setInvalid({})
      setJsonDrafts({})
      setYamlDraft(null)
      setDiffOpen(false)
      setFormResetKey((key) => key + 1)
    }
  }

  const clearEditorDraft = useCallback(() => {
    setEditedValues({})
    setDirty({})
    setInvalid({})
    setJsonDrafts({})
    setYamlDraft(null)
    setDiffOpen(false)
    setFormResetKey((key) => key + 1)
    setSnapshotConflict(false)
  }, [])

  const onDiscardOrReload = useCallback(() => {
    // Reloading a fresh snapshot re-runs the render-phase reset above; but an
    // invalidate that resolves to an identical snapshot keeps the same
    // dataUpdatedAt, so clear the local edit state here too (Discard/Reload
    // must always drop pending edits — config.js:224-231,242-251).
    clearEditorDraft()
    if (usesExternalSnapshot) {
      void onSnapshotReload?.()
    } else {
      void queryClient.invalidateQueries({ queryKey: ['config'] })
    }
  }, [clearEditorDraft, onSnapshotReload, queryClient, usesExternalSnapshot])

  // config.js:585-616 — a field edit: parse → validate JSON → diff vs loaded.
  const onFieldChange = useCallback(
    (key: string, kind: 'boolean' | 'number' | 'json' | 'string', raw: string) => {
      // config.js:594 — record the raw object-field text on every keystroke,
      // before parsing, so an invalid draft survives an unmount/remount.
      if (kind === 'json') setJsonDrafts((m) => ({ ...m, [key]: raw }))
      const parsed = parseFieldValue(kind, raw)
      if (!parsed.ok) {
        // Invalid JSON: flag it and stop (no dirty diff while unparseable). The
        // raw draft is already stored above so the textarea and the flag agree.
        setInvalid((m) => ({ ...m, [key]: true }))
        return
      }
      setInvalid((m) => {
        if (!(key in m)) return m
        const next = { ...m }
        delete next[key]
        return next
      })
      const result = computeDirty(config, key, parsed.value)
      setEditedValues((v) => ({ ...v, [key]: parsed.value }))
      setDirty((d) => {
        const next = { ...d }
        if (result.dirty) next[key] = { old: result.old, new: result.new }
        else delete next[key]
        return next
      })
      // config.js:609 — a valid edit that reverts to the loaded value is a no-op;
      // drop the draft so the field re-seeds from the canonical serialisation.
      if (kind === 'json' && !result.dirty) {
        setJsonDrafts((m) => {
          if (!(key in m)) return m
          const next = { ...m }
          delete next[key]
          return next
        })
      }
    },
    [config],
  )

  // YAML edit: dirty when the draft differs from the loaded baseline.
  const onYamlChange = useCallback(
    (text: string) => {
      // Canonicalise a reverted YAML edit back to "no draft". Otherwise a
      // later background snapshot could make the old-but-reverted text appear
      // dirty against the new baseline and raise a false conflict.
      setYamlDraft(text === baselineYaml ? null : text)
    },
    [baselineYaml],
  )

  const patchMutation = useMutation({
    mutationFn: (payload: { patches: Record<string, unknown>; expectedRevision?: string }) =>
      rpc.call<SaveResult>('config.patch', payload),
    onSuccess: (res) => {
      if (res?.restartRequired) {
        toast.info('Config saved. Gateway restart required for the change to take effect.', {
          id: 'config-save',
        })
      } else {
        toast.success('Config saved', { id: 'config-save' })
      }
      clearEditorDraft()
      void queryClient.invalidateQueries({ queryKey: ['config'] })
      void queryClient.invalidateQueries({ queryKey: SETTINGS_SNAPSHOT_QUERY_KEY })
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Save failed: ' + message, { id: 'config-save-err' })
    },
  })

  const applyMutation = useMutation({
    mutationFn: (payload: {
      config_yaml: string
      baseline_yaml: string
      expectedRevision?: string
    }) => rpc.call<SaveResult>('config.apply', payload),
    onSuccess: (res) => {
      if (res?.restartRequired) {
        toast.info('Config applied. Gateway restart required for the change to take effect.', {
          id: 'config-apply',
        })
      } else {
        toast.success('Config applied', { id: 'config-apply' })
      }
      clearEditorDraft()
      void queryClient.invalidateQueries({ queryKey: ['config'] })
      void queryClient.invalidateQueries({ queryKey: SETTINGS_SNAPSHOT_QUERY_KEY })
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Apply failed: ' + message, { id: 'config-apply-err' })
    },
  })

  const saving = patchMutation.isPending || applyMutation.isPending

  // Both draft surfaces remain mounted logically, but one may never overwrite
  // pending work from the other. The visible save bar therefore tracks either
  // draft, not only the currently selected editor.
  const formKeys = dirtyCount(dirty)
  const formPendingKeys = new Set([...Object.keys(dirty), ...Object.keys(invalid)]).size
  const invalidJson = hasInvalidJson(invalid)
  const yamlDirty = yamlHasChanges
  const count = yamlDirty ? 1 : formPendingKeys
  const barVisible = formPendingKeys > 0 || yamlDirty
  const activeEditorCanSave =
    mode === 'yaml'
      ? yamlDirty && formPendingKeys === 0
      : formKeys > 0 && !invalidJson && !yamlDirty

  // config.js:722-745 — Save: YAML mode → config.apply; form mode → config.patch
  // with the invalid-JSON gate + the no-op short-circuit.
  const onSave = useCallback(() => {
    if (writeBlocked) {
      toast.error('The config file changed outside AgentOS. Restart or reload the gateway first.', {
        id: 'config-save',
      })
      return
    }
    if (snapshotConflict) {
      toast.error('Configuration changed in another workspace. Discard and reload before saving.', {
        id: 'config-save',
      })
      return
    }
    if (mode === 'yaml') {
      if (formPendingKeys > 0) {
        toast.warning('Save or discard the pending Form changes before applying YAML.', {
          id: 'config-save',
        })
        return
      }
      const text = yamlDraft ?? baselineYaml
      if (!yamlDirty) {
        toast.info('No changes to save', { id: 'config-save' })
        return
      }
      applyMutation.mutate({
        ...buildApplyPayload(text, baselineYaml),
        ...(configRevision ? { expectedRevision: configRevision } : {}),
      })
      return
    }
    if (yamlDirty) {
      toast.warning('Save or discard the pending YAML change before saving Form fields.', {
        id: 'config-save',
      })
      return
    }
    if (invalidJson) {
      toast.error('Fix invalid JSON before saving', { id: 'config-save' })
      return
    }
    const payload = buildPatchPayload(dirty)
    if (Object.keys(payload.patches).length === 0) {
      toast.info('No changes to save', { id: 'config-save' })
      return
    }
    patchMutation.mutate({
      ...payload,
      ...(configRevision ? { expectedRevision: configRevision } : {}),
    })
  }, [
    mode,
    formPendingKeys,
    yamlDraft,
    baselineYaml,
    yamlDirty,
    invalidJson,
    dirty,
    writeBlocked,
    snapshotConflict,
    configRevision,
    applyMutation,
    patchMutation,
  ])

  // Keep the pending bar visible across editor switches so neither draft can
  // disappear from the operator's awareness.
  const yamlDirtyVisible = yamlDirty

  useEffect(() => {
    if (!barVisible) return
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => window.removeEventListener('beforeunload', warnBeforeUnload)
  }, [barVisible])

  const onReload = () => {
    if (barVisible) {
      toast.warning('Discard pending changes before reloading the configuration.', {
        id: 'config-reload',
      })
      return
    }
    onDiscardOrReload()
  }

  const configLoading = usesExternalSnapshot
    ? externalSnapshot === null && !externalSnapshotError
    : configQuery.isLoading
  const configError = usesExternalSnapshot ? externalSnapshotError : configQuery.isError
  const configReady = usesExternalSnapshot ? externalSnapshot !== null : configQuery.isSuccess

  const retryConfig = () => {
    if (usesExternalSnapshot) {
      void onSnapshotReload?.()
    } else {
      void configQuery.refetch()
    }
  }

  const onTabKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
      let nextIndex: number | null = null
      if (event.key === 'ArrowRight') nextIndex = (index + 1) % TABS.length
      if (event.key === 'ArrowLeft') nextIndex = (index - 1 + TABS.length) % TABS.length
      if (event.key === 'Home') nextIndex = 0
      if (event.key === 'End') nextIndex = TABS.length - 1
      if (nextIndex === null) return
      event.preventDefault()
      const next = TABS[nextIndex]
      if (!next) return
      setActiveTab(next.id)
      const tabs =
        event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
      tabs?.[nextIndex]?.focus()
    },
    [],
  )

  const visibleTab = search.trim() ? SEARCH_TAB : TABS.find((tab) => tab.id === activeTab)

  return (
    <div className={`cfg-stage${embedded ? ' cfg-stage--embedded' : ''}`}>
      <header className={`cfg-stage__header${embedded ? ' cfg-stage__header--embedded' : ''}`}>
        <div className="cfg-stage__title-block">
          <span className="t-label">{embedded ? 'Advanced workspace' : 'Control · Config'}</span>
          {embedded ? (
            <h2 className="cfg-stage__embedded-title">Configuration editor</h2>
          ) : (
            <h1 className="t-display">Config</h1>
          )}
          <p className="cfg-stage__subtitle">
            {embedded
              ? 'Edit the complete runtime surface with a reviewed diff before it is applied.'
              : 'Advanced gateway configuration. Use guided setup for provider, router, channels, and extras.'}
          </p>
        </div>
        <div className="cfg-stage__actions">
          <div className="cfg-mode-toggle" role="group" aria-label="Editor mode">
            <button
              type="button"
              className={`cfg-mode-btn${mode === 'form' ? ' is-active' : ''}`}
              aria-pressed={mode === 'form'}
              onClick={() => setMode('form')}
            >
              Form
            </button>
            <button
              type="button"
              className={`cfg-mode-btn${mode === 'yaml' ? ' is-active' : ''}`}
              aria-pressed={mode === 'yaml'}
              onClick={() => setMode('yaml')}
            >
              YAML
            </button>
          </div>
          {!embedded ? (
            <Button
              variant="outline"
              className="text-xs uppercase tracking-[0.14em]"
              title="Open guided setup"
              onClick={() => navigate('/setup')}
            >
              <SlidersHorizontalIcon />
              <span>Guided setup</span>
            </Button>
          ) : null}
          <Button
            variant="outline"
            className="text-xs uppercase tracking-[0.14em]"
            title="Reload config"
            onClick={onReload}
          >
            <RefreshCwIcon />
            <span>Reload</span>
          </Button>
          <Button
            className="text-xs uppercase tracking-[0.14em]"
            title="Save config"
            aria-label="Save config"
            disabled={saving || snapshotConflict || writeBlocked || !activeEditorCanSave}
            onClick={onSave}
          >
            <CheckIcon />
            <span>Save</span>
          </Button>
        </div>
      </header>

      {configLoading ? (
        <div className="cfg-state" role="status">
          Loading configuration…
        </div>
      ) : null}

      {configError ? (
        <div className="cfg-state cfg-state--error" role="alert">
          <span>Configuration could not be loaded.</span>
          <Button type="button" size="sm" variant="outline" onClick={retryConfig}>
            Retry
          </Button>
        </div>
      ) : null}

      {snapshotConflict ? (
        <div className="cfg-state cfg-state--error" role="alert">
          <span>
            Configuration changed while this draft was open. Discard the stale draft before saving
            against the latest revision.
          </span>
          <Button type="button" size="sm" variant="outline" onClick={onDiscardOrReload}>
            Discard draft &amp; reload
          </Button>
        </div>
      ) : null}

      {diskDiverged && !embedded ? (
        <div className="cfg-state cfg-state--error" role="alert">
          <span>
            The config file changed outside AgentOS. Writes are blocked until the gateway reloads or
            restarts with that file.
          </span>
          <Button type="button" size="sm" variant="outline" onClick={onDiscardOrReload}>
            Refresh state
          </Button>
        </div>
      ) : null}

      {configReady && mode === 'form' ? (
        <div className="cfg-form">
          <div className="cfg-toolbar">
            <div className="cfg-tabs" role="tablist" aria-label="Config sections">
              {TABS.map((tab, index) => (
                <button
                  key={tab.id}
                  type="button"
                  role="tab"
                  aria-selected={tab.id === activeTab}
                  tabIndex={tab.id === activeTab ? 0 : -1}
                  className={`cfg-tab${tab.id === activeTab ? ' is-active' : ''}`}
                  onClick={() => setActiveTab(tab.id)}
                  onKeyDown={(event) => onTabKeyDown(event, index)}
                >
                  {tab.label}
                </button>
              ))}
            </div>
            <label className="cfg-search">
              <SearchIcon className="cfg-search__icon" aria-hidden="true" />
              <input
                className="cfg-search__input"
                type="search"
                aria-label="Search config"
                placeholder="Search keys & values…"
                autoComplete="off"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
          </div>
          {visibleTab ? (
            <FormTab
              key={visibleTab.id}
              config={config}
              tab={visibleTab}
              search={search.toLowerCase()}
              editedValues={editedValues}
              dirty={dirty}
              invalid={invalid}
              jsonDrafts={jsonDrafts}
              resetKey={formResetKey}
              onChange={onFieldChange}
            />
          ) : null}
        </div>
      ) : null}

      {configReady && mode === 'yaml' ? (
        <div className="cfg-yaml">
          <textarea
            className="cfg-input cfg-yaml__area"
            aria-label="YAML editor"
            spellCheck={false}
            value={yamlDraft ?? baselineYaml}
            onChange={(e) => onYamlChange(e.target.value)}
          />
        </div>
      ) : null}

      {barVisible ? (
        <StickyBar
          count={count}
          // config.js:686 — the diff view keys off yamlDirtyVisible, not the raw
          // mode: in YAML mode with only pending form edits it still shows the
          // per-field form diff rows.
          yamlMode={yamlDirtyVisible}
          dirty={dirty}
          invalid={invalid}
          diffOpen={diffOpen}
          onToggleDiff={() => setDiffOpen((o) => !o)}
          onDiscard={onDiscardOrReload}
          onSave={onSave}
          saving={saving}
          saveDisabled={saving || snapshotConflict || writeBlocked || !activeEditorCanSave}
        />
      ) : null}
    </div>
  )
}
