import { useMutation, useQueryClient } from '@tanstack/react-query'
import { DatabaseZap, Eraser, RefreshCw } from 'lucide-react'
import { useEffect, useId, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import type { MemoryStatusWire } from '@/views/memory/logic'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { Button } from '~/components/ui/button'
import { Switch } from '~/components/ui/switch'
import { t, type MessageKey } from '~/i18n'
import { Card, Choices, Disclosure, Notice, Row, Segmented, type ChoiceOption } from '../parts'
import { PendingRestartNotice } from '../PendingRestartNotice'
import { useConfigSnapshot, withRevision } from '../use-snapshot'
import { MEMORY_KEY, reportSaveError, useConnected, useFacts, useMemoryAction } from './api'
import { useDraft } from './drafts'
import { EmbeddingCard } from './EmbeddingCard'
import { ForgetSheet } from './ForgetSheet'
import {
  AGENT_ID,
  REVIEW_INTERVALS,
  limitsBelowUse,
  limitsCheck,
  limitsDraft,
  limitsPatches,
  parseSeconds,
  parseWhole,
  recallDraft,
  recallPatches,
  reviewDraft,
  reviewPatches,
  type LimitsDraft,
  type Patches,
  type RecallDraft,
  type RecallMode,
  type ReviewDraft,
} from './logic'
import { Confirm, Spinner, count, counted, fill, unitFor } from './ui'

/**
 * Setup: how memory is searched, recall before each reply, learning from
 * conversations, the size limits, and maintenance. Each settings card keeps
 * its own draft with Save and Revert in its foot; nothing is written until
 * Save, and a draft survives switching tabs.
 */
export function SetupTab({ active }: { active: boolean }) {
  const { connected, query, snapshot, reload } = useConfigSnapshot()
  const queryClient = useQueryClient()
  const [rebuilding, setRebuilding] = useState(false)
  // Others write the same config, and the agent fills the lists the limits
  // are checked against: read both again each time Setup is opened.
  useEffect(() => {
    if (!active || !connected) return
    void reload()
    void queryClient.invalidateQueries({ queryKey: [...MEMORY_KEY, 'curated'] })
  }, [active, connected, reload, queryClient])
  return (
    <>
      {!connected ? (
        <p className="mem-quiet">{t('settings.memory.setup.offline')}</p>
      ) : query.isError ? (
        <Notice tone="danger">{t('settings.loadFailed')}</Notice>
      ) : !snapshot ? (
        <Spinner />
      ) : (
        <>
          {snapshot.writeBlocked ? (
            <Notice tone="danger">{t('settings.writeBlocked')}</Notice>
          ) : snapshot.pendingRestart ? (
            <PendingRestartNotice />
          ) : null}
          <EmbeddingCard
            snapshot={snapshot}
            reload={reload}
            onRebuild={() => setRebuilding(true)}
          />
          <RecallCard snapshot={snapshot} reload={reload} />
          <ReviewCard snapshot={snapshot} reload={reload} />
          <LimitsCard snapshot={snapshot} reload={reload} />
        </>
      )}
      <MaintenanceCard rebuilding={rebuilding} setRebuilding={setRebuilding} />
    </>
  )
}

/** A card's Save: dot-path `config.patch` with the snapshot revision. */
function usePatch(
  snapshot: SettingsSnapshot,
  reload: () => Promise<void>,
  settle: () => void,
  toastId: string,
) {
  const rpc = useRpc()
  return useMutation({
    mutationFn: (patches: Patches) =>
      rpc.call<{ restartRequired?: boolean }>('config.patch', withRevision(snapshot, { patches })),
    onSuccess: async (res) => {
      settle()
      toast.success(
        res?.restartRequired ? t('settings.restartRequired') : t('settings.memory.applied'),
        { id: toastId },
      )
      await reload()
    },
    onError: (err) => reportSaveError(err, reload, `${toastId}-err`),
  })
}

function SaveFoot({
  dirty,
  canSave,
  saving,
  onRevert,
  onSave,
}: {
  dirty: boolean
  canSave: boolean
  saving: boolean
  onRevert: () => void
  onSave: () => void
}) {
  return (
    <>
      <Button disabled={!dirty || saving} onClick={onRevert}>
        {t('settings.revert')}
      </Button>
      <Button variant="primary" disabled={!canSave || saving} onClick={onSave}>
        {saving ? t('settings.memory.saving') : t('settings.save')}
      </Button>
    </>
  )
}

function FootNote({ problem, dirty }: { problem?: string | null; dirty: boolean }) {
  if (problem) return <span className="mem-problem">{problem}</span>
  return <>{dirty ? t('settings.memory.unsaved') : t('settings.memory.live')}</>
}

/** A number field with its unit after it. */
function UnitInput({
  id,
  value,
  unit,
  invalid,
  disabled,
  onChange,
  step,
}: {
  id: string
  value: string
  unit: string
  invalid?: boolean
  disabled?: boolean
  onChange: (value: string) => void
  step?: string
}) {
  return (
    <span className="mem-unit">
      <input
        id={id}
        className="mac-input"
        type="number"
        inputMode="numeric"
        min={0}
        step={step}
        value={value}
        disabled={disabled}
        data-invalid={invalid ? 'true' : undefined}
        aria-invalid={invalid || undefined}
        onChange={(e) => onChange(e.target.value)}
      />
      <span>{unit}</span>
    </span>
  )
}

const MODE_COPY: Record<RecallMode, { title: MessageKey; blurb: MessageKey; tag?: MessageKey }> = {
  escalate: {
    title: 'settings.memory.recall.mode.escalate',
    blurb: 'settings.memory.recall.mode.escalate.blurb',
    tag: 'settings.memory.embed.tag.recommended',
  },
  always: {
    title: 'settings.memory.recall.mode.always',
    blurb: 'settings.memory.recall.mode.always.blurb',
  },
  off: {
    title: 'settings.memory.recall.mode.off',
    blurb: 'settings.memory.recall.mode.off.blurb',
  },
}

function RecallCard({
  snapshot,
  reload,
}: {
  snapshot: SettingsSnapshot
  reload: () => Promise<void>
}) {
  const saved = recallDraft(snapshot.config)
  const { draft, setDraft, revert, settle } = useDraft<RecallDraft>('recall', saved)
  const save = usePatch(snapshot, reload, settle, 'stg-memory-recall')
  const patches = recallPatches(snapshot.config, draft)
  const dirty = patches === null || Object.keys(patches).length > 0
  const blocked = Boolean(snapshot.writeBlocked) || save.isPending
  const ids = { chars: useId(), timeout: useId(), provider: useId(), model: useId() }
  const charsBad = parseWhole(draft.maxInjectedChars, 200, 8000) === null
  const timeoutBad = parseSeconds(draft.deepTimeoutSeconds, 120) === null
  const options: ChoiceOption<RecallMode>[] = (['escalate', 'always', 'off'] as const).map((m) => ({
    value: m,
    title: t(MODE_COPY[m].title),
    blurb: t(MODE_COPY[m].blurb),
    tag: MODE_COPY[m].tag ? t(MODE_COPY[m].tag!) : undefined,
    tagTone: 'primary',
  }))
  const problem = charsBad
    ? t('settings.memory.recall.invalid.chars')
    : timeoutBad
      ? t('settings.memory.recall.invalid.timeout')
      : null

  return (
    <Card
      title={t('settings.memory.recall.card.title')}
      blurb={t('settings.memory.recall.card.blurb')}
      footNote={<FootNote problem={problem} dirty={dirty} />}
      foot={
        <SaveFoot
          dirty={dirty}
          canSave={patches !== null && Object.keys(patches).length > 0 && !blocked}
          saving={save.isPending}
          onRevert={revert}
          onSave={() => patches && save.mutate(patches)}
        />
      }
    >
      <Row
        label={t('settings.memory.recall.enabled')}
        help={t('settings.memory.recall.enabled.help')}
      >
        <Switch
          checked={draft.enabled}
          disabled={blocked}
          aria-label={t('settings.memory.recall.enabled')}
          onCheckedChange={(enabled) => setDraft((d) => ({ ...d, enabled }))}
        />
      </Row>
      <p className="mem-sub">{t('settings.memory.recall.mode')}</p>
      <Choices
        label={t('settings.memory.recall.mode')}
        value={draft.mode}
        options={options}
        columns={3}
        disabled={blocked || !draft.enabled}
        onChange={(mode) => setDraft((d) => ({ ...d, mode }))}
      />
      <Disclosure
        title={t('settings.memory.advanced')}
        hint={fill('settings.memory.recall.advanced.hint', {
          chars: count(Number(saved.maxInjectedChars)),
          seconds: saved.deepTimeoutSeconds,
          model: saved.model || t('settings.memory.recall.model.inherit'),
        })}
      >
        <Row
          label={t('settings.memory.recall.maxChars')}
          help={t('settings.memory.recall.maxChars.help')}
          htmlFor={ids.chars}
        >
          <UnitInput
            id={ids.chars}
            value={draft.maxInjectedChars}
            unit={t('settings.memory.unit.chars')}
            invalid={charsBad}
            disabled={blocked || !draft.enabled}
            onChange={(maxInjectedChars) => setDraft((d) => ({ ...d, maxInjectedChars }))}
          />
        </Row>
        <Row
          label={t('settings.memory.recall.timeout')}
          help={t('settings.memory.recall.timeout.help')}
          htmlFor={ids.timeout}
        >
          <UnitInput
            id={ids.timeout}
            value={draft.deepTimeoutSeconds}
            unit={unitFor(
              draft.deepTimeoutSeconds,
              'settings.memory.unit.second',
              'settings.memory.unit.seconds',
            )}
            step="0.5"
            invalid={timeoutBad}
            disabled={blocked || !draft.enabled}
            onChange={(deepTimeoutSeconds) => setDraft((d) => ({ ...d, deepTimeoutSeconds }))}
          />
        </Row>
        <Row
          label={t('settings.memory.recall.model')}
          help={t('settings.memory.recall.model.help')}
          htmlFor={ids.model}
        >
          <input
            id={ids.model}
            className="mac-input"
            data-mono="true"
            value={draft.model}
            placeholder={t('settings.memory.recall.model.inherit')}
            autoComplete="off"
            spellCheck={false}
            disabled={blocked || !draft.enabled}
            onChange={(e) => setDraft((d) => ({ ...d, model: e.target.value }))}
          />
        </Row>
        <Row
          label={t('settings.memory.recall.provider')}
          help={t('settings.memory.recall.provider.help')}
          htmlFor={ids.provider}
        >
          <input
            id={ids.provider}
            className="mac-input"
            data-mono="true"
            value={draft.provider}
            placeholder={t('settings.memory.recall.model.inherit')}
            autoComplete="off"
            spellCheck={false}
            disabled={blocked || !draft.enabled}
            onChange={(e) => setDraft((d) => ({ ...d, provider: e.target.value }))}
          />
        </Row>
      </Disclosure>
    </Card>
  )
}

function ReviewCard({
  snapshot,
  reload,
}: {
  snapshot: SettingsSnapshot
  reload: () => Promise<void>
}) {
  const saved = reviewDraft(snapshot.config)
  const { draft, setDraft, revert, settle } = useDraft<ReviewDraft>('review', saved)
  const save = usePatch(snapshot, reload, settle, 'stg-memory-review')
  const patches = reviewPatches(snapshot.config, draft)
  const dirty = patches === null || Object.keys(patches).length > 0
  const blocked = Boolean(snapshot.writeBlocked) || save.isPending
  const preset = (REVIEW_INTERVALS as readonly string[]).includes(draft.interval)
  const [custom, setCustom] = useState(!preset)
  const ids = { interval: useId(), timeout: useId() }
  const intervalBad = draft.enabled && parseWhole(draft.interval, 1, 1000) === null
  const timeoutBad = parseSeconds(draft.timeoutSeconds, 3600) === null
  const problem = intervalBad
    ? t('settings.memory.review.invalid.interval')
    : timeoutBad
      ? t('settings.memory.review.invalid.timeout')
      : null
  const off = blocked || !draft.enabled

  return (
    <Card
      title={t('settings.memory.review.title')}
      blurb={t('settings.memory.review.blurb')}
      footNote={<FootNote problem={problem} dirty={dirty} />}
      foot={
        <SaveFoot
          dirty={dirty}
          canSave={patches !== null && Object.keys(patches).length > 0 && !blocked}
          saving={save.isPending}
          onRevert={() => {
            revert()
            setCustom(!(REVIEW_INTERVALS as readonly string[]).includes(saved.interval))
          }}
          onSave={() => patches && save.mutate(patches)}
        />
      }
    >
      <Row
        label={t('settings.memory.review.enabled')}
        help={t('settings.memory.review.enabled.help')}
      >
        <Switch
          checked={draft.enabled}
          disabled={blocked}
          aria-label={t('settings.memory.review.enabled')}
          onCheckedChange={(enabled) => setDraft((d) => ({ ...d, enabled }))}
        />
      </Row>
      <Row
        label={t('settings.memory.review.interval')}
        help={t('settings.memory.review.interval.help')}
        htmlFor={custom ? ids.interval : undefined}
      >
        <span className="mem-inline">
          <Segmented
            label={t('settings.memory.review.interval')}
            value={custom ? 'custom' : draft.interval}
            disabled={off}
            options={[
              ...REVIEW_INTERVALS.map((n) => ({ value: n as string, label: n })),
              { value: 'custom', label: t('settings.memory.review.custom') },
            ]}
            onChange={(value) => {
              if (value === 'custom') {
                setCustom(true)
                return
              }
              setCustom(false)
              setDraft((d) => ({ ...d, interval: value }))
            }}
          />
          {custom ? (
            <UnitInput
              id={ids.interval}
              value={draft.interval}
              unit={unitFor(
                draft.interval,
                'settings.memory.unit.message',
                'settings.memory.unit.messages',
              )}
              disabled={off}
              invalid={intervalBad}
              onChange={(interval) => setDraft((d) => ({ ...d, interval }))}
            />
          ) : (
            <span className="mem-unit-text">{t('settings.memory.unit.messages')}</span>
          )}
        </span>
      </Row>
      <Row
        label={t('settings.memory.review.compaction')}
        help={t('settings.memory.review.compaction.help')}
      >
        <Switch
          checked={draft.beforeCompaction}
          disabled={off}
          aria-label={t('settings.memory.review.compaction')}
          onCheckedChange={(beforeCompaction) => setDraft((d) => ({ ...d, beforeCompaction }))}
        />
      </Row>
      <Disclosure
        title={t('settings.memory.advanced')}
        hint={fill('settings.memory.review.advanced.hint', { seconds: saved.timeoutSeconds })}
      >
        <Row
          label={t('settings.memory.review.timeout')}
          help={t('settings.memory.review.timeout.help')}
          htmlFor={ids.timeout}
        >
          <UnitInput
            id={ids.timeout}
            value={draft.timeoutSeconds}
            unit={unitFor(
              draft.timeoutSeconds,
              'settings.memory.unit.second',
              'settings.memory.unit.seconds',
            )}
            invalid={timeoutBad}
            disabled={off}
            onChange={(timeoutSeconds) => setDraft((d) => ({ ...d, timeoutSeconds }))}
          />
        </Row>
      </Disclosure>
    </Card>
  )
}

function LimitsCard({
  snapshot,
  reload,
}: {
  snapshot: SettingsSnapshot
  reload: () => Promise<void>
}) {
  const saved = limitsDraft(snapshot.config)
  const { draft, setDraft, revert, settle } = useDraft<LimitsDraft>('limits', saved)
  const save = usePatch(snapshot, reload, settle, 'stg-memory-limits')
  const work = useFacts('memory').data
  const you = useFacts('user').data
  const used = { work: work?.charCount, you: you?.charCount }
  const check = limitsCheck(draft)
  const below = limitsBelowUse(snapshot.config, draft, used)
  const patches = limitsPatches(snapshot.config, draft, used)
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved)
  const blocked = Boolean(snapshot.writeBlocked) || save.isPending
  const ids = { work: useId(), you: useId(), inject: useId() }
  const tooSmall = check.inject !== null && check.needed !== null && check.inject < check.needed
  const invalid = check.work === null || check.you === null || check.inject === null

  const usedHelp = (
    used: number | undefined,
    byDefault: number,
    fallback: MessageKey,
  ): ReactNode =>
    used !== undefined
      ? fill('settings.memory.limits.used', { used: count(used), default: count(byDefault) })
      : t(fallback)

  return (
    <Card
      title={t('settings.memory.limits.title')}
      blurb={t('settings.memory.limits.blurb')}
      footNote={
        <FootNote
          problem={
            invalid
              ? t('settings.memory.limits.invalid')
              : below.length > 0
                ? t('settings.memory.limits.belowUse.short')
                : null
          }
          dirty={dirty}
        />
      }
      foot={
        <SaveFoot
          dirty={dirty}
          canSave={patches !== null && Object.keys(patches).length > 0 && !blocked}
          saving={save.isPending}
          onRevert={revert}
          onSave={() => patches && save.mutate(patches)}
        />
      }
    >
      <Row
        label={t('settings.memory.work.title')}
        help={usedHelp(work?.charCount, 4000, 'settings.memory.limits.work.help')}
        htmlFor={ids.work}
      >
        <UnitInput
          id={ids.work}
          value={draft.workChars}
          unit={t('settings.memory.unit.chars')}
          invalid={check.work === null || below.some((b) => b.list === 'work')}
          disabled={blocked}
          onChange={(workChars) => setDraft((d) => ({ ...d, workChars }))}
        />
      </Row>
      <Row
        label={t('settings.memory.you.title')}
        help={usedHelp(you?.charCount, 2000, 'settings.memory.limits.you.help')}
        htmlFor={ids.you}
      >
        <UnitInput
          id={ids.you}
          value={draft.youChars}
          unit={t('settings.memory.unit.chars')}
          invalid={check.you === null || below.some((b) => b.list === 'you')}
          disabled={blocked}
          onChange={(youChars) => setDraft((d) => ({ ...d, youChars }))}
        />
      </Row>
      <Row
        label={t('settings.memory.limits.inject')}
        help={t('settings.memory.limits.inject.help')}
        htmlFor={ids.inject}
      >
        <UnitInput
          id={ids.inject}
          value={draft.injectLimit}
          unit={t('settings.memory.unit.chars')}
          invalid={check.inject === null || tooSmall}
          disabled={blocked}
          onChange={(injectLimit) => setDraft((d) => ({ ...d, injectLimit }))}
        />
      </Row>
      {below.map((b) => (
        <div className="mem-card-notice" key={b.list}>
          <Notice
            tone="warn"
            action={
              <Button
                disabled={blocked}
                onClick={() =>
                  setDraft((d) =>
                    b.list === 'work'
                      ? { ...d, workChars: String(b.used) }
                      : { ...d, youChars: String(b.used) },
                  )
                }
              >
                {fill('settings.memory.limits.use', { needed: count(b.used) })}
              </Button>
            }
          >
            {fill('settings.memory.limits.belowUse', {
              list: t(
                b.list === 'work' ? 'settings.memory.work.title' : 'settings.memory.you.title',
              ),
              used: counted(b.used, 'char'),
              limit: count(b.limit),
              min: count(b.used),
            })}
          </Notice>
        </div>
      ))}
      {tooSmall && check.needed !== null ? (
        <div className="mem-card-notice">
          <Notice
            tone="warn"
            action={
              <Button
                disabled={blocked}
                onClick={() => setDraft((d) => ({ ...d, injectLimit: String(check.needed) }))}
              >
                {fill('settings.memory.limits.use', { needed: count(check.needed) })}
              </Button>
            }
          >
            {fill('settings.memory.limits.tooSmall', { needed: count(check.needed) })}
          </Notice>
        </div>
      ) : null}
    </Card>
  )
}

function MaintenanceCard({
  rebuilding,
  setRebuilding,
}: {
  rebuilding: boolean
  setRebuilding: (open: boolean) => void
}) {
  const connected = useConnected()
  const action = useMemoryAction()
  const [forgetting, setForgetting] = useState(false)

  async function reindex(force: boolean) {
    const res = await action.run(force ? 'rebuild' : 'reindex', () =>
      action.rpc.call<MemoryStatusWire>('memory.index', { agentId: AGENT_ID, force }),
    )
    if (force) setRebuilding(false)
    if (!res) return
    toast.success(
      fill(force ? 'settings.memory.maint.rebuilt' : 'settings.memory.maint.reindexed', {
        chunks: counted(res.chunkCount ?? 0, 'passage'),
        files: counted(res.fileCount ?? 0, 'file'),
      }),
      { id: 'stg-memory-index' },
    )
  }

  return (
    <Card title={t('settings.memory.maint.title')} blurb={t('settings.memory.maint.blurb')}>
      <Row
        label={t('settings.memory.maint.reindex')}
        help={t('settings.memory.maint.reindex.help')}
      >
        <Button disabled={!connected || action.busy !== null} onClick={() => void reindex(false)}>
          <RefreshCw
            className={action.busy === 'reindex' ? 'stg-spin size-3.5' : 'size-3.5'}
            strokeWidth={1.75}
            aria-hidden
          />
          {action.busy === 'reindex'
            ? t('settings.memory.maint.reindexing')
            : t('settings.memory.maint.reindex.button')}
        </Button>
      </Row>
      <Row
        label={t('settings.memory.maint.rebuild')}
        help={t('settings.memory.maint.rebuild.help')}
      >
        <Button disabled={!connected || action.busy !== null} onClick={() => setRebuilding(true)}>
          <DatabaseZap className="size-3.5" strokeWidth={1.75} aria-hidden />
          {action.busy === 'rebuild'
            ? t('settings.memory.maint.rebuilding')
            : t('settings.memory.maint.rebuild.button')}
        </Button>
      </Row>
      <Row label={t('settings.memory.maint.forget')} help={t('settings.memory.maint.forget.help')}>
        <Button disabled={!connected} onClick={() => setForgetting(true)}>
          <Eraser className="size-3.5" strokeWidth={1.75} aria-hidden />
          {t('settings.memory.maint.forget.button')}
        </Button>
      </Row>

      {rebuilding ? (
        <Confirm
          title={t('settings.memory.maint.rebuild.confirm.title')}
          body={<p>{t('settings.memory.maint.rebuild.confirm.body')}</p>}
          confirm={
            action.busy === 'rebuild'
              ? t('settings.memory.maint.rebuilding')
              : t('settings.memory.maint.rebuild.button')
          }
          busy={action.busy === 'rebuild'}
          onCancel={() => setRebuilding(false)}
          onConfirm={() => void reindex(true)}
        />
      ) : null}
      {forgetting ? <ForgetSheet onClose={() => setForgetting(false)} /> : null}
    </Card>
  )
}
