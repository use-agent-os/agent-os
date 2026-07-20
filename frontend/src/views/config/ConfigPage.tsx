import './config.css'
import { useCallback, useEffect, useId, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CheckIcon,
  HelpCircleIcon,
  RefreshCwIcon,
  SearchIcon,
  SlidersHorizontalIcon,
} from 'lucide-react'
import { toast } from 'sonner'
import { AsciiField } from '@/components/AsciiField'
import { Button } from '@/components/ui/button'
import { useRpc } from '@/app/providers'
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

type EditorMode = 'form' | 'yaml'

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
  onChange,
}: {
  configKey: string
  value: unknown
  groupId: string
  dirty: boolean
  invalid: boolean
  onChange: (key: string, kind: 'boolean' | 'number' | 'json' | 'string', raw: string) => void
}) {
  const inputId = useId()
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
    const jsonStr = JSON.stringify(value, null, 2)
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
          type={sensitive ? 'password' : 'text'}
          value={String(value ?? '')}
          onChange={(e) => onChange(configKey, 'string', e.target.value)}
        />
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
  resetKey,
  onChange,
}: {
  config: ConfigData
  tab: TabDef
  search: string
  editedValues: Record<string, unknown>
  dirty: DirtyMap
  invalid: InvalidJsonMap
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
            <h3 className="cfg-group__title t-label">{group.title}</h3>
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
  diffOpen,
  onToggleDiff,
  onDiscard,
  onSave,
  saving,
}: {
  count: number
  yamlMode: boolean
  dirty: DirtyMap
  diffOpen: boolean
  onToggleDiff: () => void
  onDiscard: () => void
  onSave: () => void
  saving: boolean
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
        <Button type="button" size="sm" disabled={saving} onClick={onSave}>
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
            Object.entries(dirty).map(([key, { old: oldV, new: newV }]) => (
              <div className="cfg-diff-row" key={key}>
                <span className="cfg-diff-row__key t-data">{key}</span>
                <span className="cfg-diff-row__old">{summariseDiffValue(oldV)}</span>
                <span className="cfg-diff-row__arrow" aria-hidden="true">
                  {'->'}
                </span>
                <span className="cfg-diff-row__new">{summariseDiffValue(newV)}</span>
              </div>
            ))
          )}
        </div>
      ) : null}
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────
export function ConfigPage() {
  const rpc = useRpc()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [mode, setMode] = useState<EditorMode>('form')
  const [activeTab, setActiveTab] = useState<string>('core')
  const [search, setSearch] = useState('')
  const [diffOpen, setDiffOpen] = useState(false)

  // Form-mode edit state (all derived against the loaded config snapshot).
  const [editedValues, setEditedValues] = useState<Record<string, unknown>>({})
  const [dirty, setDirty] = useState<DirtyMap>({})
  const [invalid, setInvalid] = useState<InvalidJsonMap>({})

  // YAML-mode draft state (baseline = objToYaml(loaded config)).
  const [yamlDraft, setYamlDraft] = useState<string | null>(null)

  useEffect(() => {
    document.title = 'Config - AgentOS Control'
  }, [])

  const configQuery = useQuery<ConfigData>({
    queryKey: ['config'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<ConfigData>('config.get')
      return data ?? {}
    },
    refetchOnWindowFocus: false,
  })

  // config.js:305 — load-failure toast (stable id so repeats dedupe).
  useEffect(() => {
    if (configQuery.isError) {
      const err = configQuery.error
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to load config: ' + message, { id: 'config-load-err' })
    }
  }, [configQuery.isError, configQuery.error])

  const config = useMemo(() => configQuery.data ?? {}, [configQuery.data])
  const baselineYaml = useMemo(() => objToYaml(config), [config])

  // Reset all edit state whenever a fresh config snapshot arrives (load / reload
  // / post-save). config.js:295-305,732,742 blanked _dirty/_invalidJson/_yamlDraft.
  // Done as a render-phase reset keyed on the query's dataUpdatedAt (React's
  // supported "adjust state when a prop/derived value changes" pattern) rather
  // than an effect, so the blank state is applied before paint with no cascading
  // second render.
  const [lastSnapshotAt, setLastSnapshotAt] = useState(0)
  const configUpdatedAt = configQuery.dataUpdatedAt
  if (configUpdatedAt && configUpdatedAt !== lastSnapshotAt) {
    setLastSnapshotAt(configUpdatedAt)
    setEditedValues({})
    setDirty({})
    setInvalid({})
    setYamlDraft(null)
    setDiffOpen(false)
  }

  const onDiscardOrReload = useCallback(() => {
    // Reloading a fresh snapshot re-runs the render-phase reset above; but an
    // invalidate that resolves to an identical snapshot keeps the same
    // dataUpdatedAt, so clear the local edit state here too (Discard/Reload
    // must always drop pending edits — config.js:224-231,242-251).
    setEditedValues({})
    setDirty({})
    setInvalid({})
    setYamlDraft(null)
    setDiffOpen(false)
    void queryClient.invalidateQueries({ queryKey: ['config'] })
  }, [queryClient])

  // config.js:585-616 — a field edit: parse → validate JSON → diff vs loaded.
  const onFieldChange = useCallback(
    (key: string, kind: 'boolean' | 'number' | 'json' | 'string', raw: string) => {
      const parsed = parseFieldValue(kind, raw)
      if (!parsed.ok) {
        // Invalid JSON: flag it and stop (no dirty diff while unparseable).
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
    },
    [config],
  )

  // YAML edit: dirty when the draft differs from the loaded baseline.
  const onYamlChange = useCallback((text: string) => {
    setYamlDraft(text)
  }, [])

  const yamlDirty = yamlDraft !== null && yamlDraft !== baselineYaml

  const patchMutation = useMutation({
    mutationFn: (payload: { patches: Record<string, unknown> }) =>
      rpc.call<SaveResult>('config.patch', payload),
    onSuccess: (res) => {
      if (res?.restartRequired) {
        toast.info('Config saved. Gateway restart required for the change to take effect.', {
          id: 'config-save',
        })
      } else {
        toast.success('Config saved', { id: 'config-save' })
      }
      void queryClient.invalidateQueries({ queryKey: ['config'] })
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Save failed: ' + message, { id: 'config-save-err' })
    },
  })

  const applyMutation = useMutation({
    mutationFn: (payload: { config_yaml: string; baseline_yaml: string }) =>
      rpc.call<SaveResult>('config.apply', payload),
    onSuccess: (res) => {
      if (res?.restartRequired) {
        toast.info('Config applied. Gateway restart required for the change to take effect.', {
          id: 'config-apply',
        })
      } else {
        toast.success('Config applied', { id: 'config-apply' })
      }
      void queryClient.invalidateQueries({ queryKey: ['config'] })
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Apply failed: ' + message, { id: 'config-apply-err' })
    },
  })

  const saving = patchMutation.isPending || applyMutation.isPending

  // config.js:722-745 — Save: YAML mode → config.apply; form mode → config.patch
  // with the invalid-JSON gate + the no-op short-circuit.
  const onSave = useCallback(() => {
    if (mode === 'yaml') {
      const text = yamlDraft ?? baselineYaml
      applyMutation.mutate(buildApplyPayload(text, baselineYaml))
      return
    }
    if (hasInvalidJson(invalid)) {
      toast.error('Fix invalid JSON before saving', { id: 'config-save' })
      return
    }
    const payload = buildPatchPayload(dirty)
    if (Object.keys(payload.patches).length === 0) {
      toast.info('No changes to save', { id: 'config-save' })
      return
    }
    patchMutation.mutate(payload)
  }, [mode, yamlDraft, baselineYaml, invalid, dirty, applyMutation, patchMutation])

  const count = mode === 'yaml' ? (yamlDirty ? 1 : 0) : dirtyCount(dirty)
  const barVisible = count > 0

  return (
    <div className="cfg-stage">
      <header className="cfg-stage__header">
        <AsciiField />
        <div className="cfg-stage__title-block">
          <span className="t-label">Control · Config</span>
          <h2 className="t-display">Config</h2>
          <p className="cfg-stage__subtitle">
            Advanced gateway configuration. Use guided setup for provider, router, channels, and
            extras.
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
          <Button
            variant="outline"
            className="text-xs uppercase tracking-[0.14em]"
            title="Open guided setup"
            onClick={() => navigate('/setup')}
          >
            <SlidersHorizontalIcon />
            <span>Guided setup</span>
          </Button>
          <Button
            variant="outline"
            className="text-xs uppercase tracking-[0.14em]"
            title="Reload config"
            onClick={onDiscardOrReload}
          >
            <RefreshCwIcon />
            <span>Reload</span>
          </Button>
          <Button
            className="text-xs uppercase tracking-[0.14em]"
            title="Save config"
            aria-label="Save config"
            disabled={saving}
            onClick={onSave}
          >
            <CheckIcon />
            <span>Save</span>
          </Button>
        </div>
      </header>

      {mode === 'form' ? (
        <div className="cfg-form">
          <div className="cfg-toolbar">
            <div className="cfg-tabs" role="tablist" aria-label="Config sections">
              {TABS.map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  role="tab"
                  aria-selected={tab.id === activeTab}
                  className={`cfg-tab${tab.id === activeTab ? ' is-active' : ''}`}
                  onClick={() => setActiveTab(tab.id)}
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
                placeholder="Search keys & values…"
                autoComplete="off"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
          </div>
          {TABS.filter((t) => t.id === activeTab).map((tab) => (
            <FormTab
              key={tab.id}
              config={config}
              tab={tab}
              search={search.toLowerCase()}
              editedValues={editedValues}
              dirty={dirty}
              invalid={invalid}
              resetKey={lastSnapshotAt}
              onChange={onFieldChange}
            />
          ))}
        </div>
      ) : (
        <div className="cfg-yaml">
          <textarea
            className="cfg-input cfg-yaml__area"
            aria-label="YAML editor"
            spellCheck={false}
            value={yamlDraft ?? baselineYaml}
            onChange={(e) => onYamlChange(e.target.value)}
          />
        </div>
      )}

      {barVisible ? (
        <StickyBar
          count={count}
          yamlMode={mode === 'yaml'}
          dirty={dirty}
          diffOpen={diffOpen}
          onToggleDiff={() => setDiffOpen((o) => !o)}
          onDiscard={onDiscardOrReload}
          onSave={onSave}
          saving={saving}
        />
      ) : null}
    </div>
  )
}
