import '../overview.css'
import { useQuery } from '@tanstack/react-query'
import { useRpc } from '@/app/providers'
import { ENV_QUERY_KEY, type EnvListResponse } from '@/views/env/logic'
import { skillStats, type RawSkill } from '@/views/skills/logic'
import { gatewayUrl } from '@shared/settings'
import { Button } from '~/components/ui/button'
import { t, type MessageKey } from '~/i18n'
import { useGateway } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import { useWalletStatus } from '~/stores/trading'
import { useUpdates } from '~/stores/updates'
import { useKeyFacts } from '../capabilities/key-context'
import { Checklist } from '../overview/Checklist'
import { Hero } from '../overview/Hero'
import { jumpTo } from '../overview/jumps'
import {
  hostOf,
  setupItems,
  summarizeSetup,
  type SetupInput,
  type SetupItem,
  type SetupSummary,
} from '../overview/setup-items'
import { Head, Notice, Pill, type Tone } from '../parts'
import { PendingRestartNotice } from '../PendingRestartNotice'
import { useConfigSnapshot } from '../use-snapshot'

function fill(key: MessageKey, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce(
    (text, [name, value]) => text.replace(`{${name}}`, String(value)),
    t(key),
  )
}

interface SkillsListResponse {
  skills?: RawSkill[]
}

/** The facts the checklist reads besides the snapshot, each from its own RPC. */
function useSideFacts(connected: boolean, tradingOn: boolean) {
  const rpc = useRpc()
  // Same keys and shapes as the Skills and Environment sections: one cache.
  const skills = useQuery<RawSkill[]>({
    queryKey: ['skills'],
    enabled: connected,
    refetchOnWindowFocus: false,
    queryFn: async () => {
      const data = await rpc.call<SkillsListResponse>('skills.list', {})
      return data.skills ?? []
    },
  })
  const env = useQuery({
    queryKey: ENV_QUERY_KEY,
    enabled: connected,
    refetchOnWindowFocus: false,
    queryFn: () => rpc.call<EnvListResponse>('env.list', {}),
  })
  const wallet = useWalletStatus(connected && tradingOn)

  const skillFacts: SetupInput['skills'] = skills.isError
    ? null
    : skills.data
      ? (({ total, needs }) => ({ total, needs }))(skillStats(skills.data))
      : undefined
  const envFacts: SetupInput['env'] = env.isError
    ? null
    : env.data
      ? { missing: (env.data.vars ?? []).filter((v) => v.missing).map((v) => v.name) }
      : undefined
  const walletFacts: SetupInput['wallet'] = wallet.isError
    ? null
    : wallet.data
      ? { initialized: wallet.data.initialized, walletCount: wallet.data.walletCount }
      : undefined
  return { skills: skillFacts, env: envFacts, wallet: walletFacts }
}

function useUpdateFacts(external: boolean): SetupInput['updates'] {
  const engine = useUpdates((s) => s.engine)
  const app = useUpdates((s) => s.app)
  const loaded = useUpdates((s) => s.loaded)
  if (!loaded) return undefined
  // An external gateway's engine is not the one installed here: only the app counts.
  const engineOutdated = !external && engine.availability === 'outdated'
  return {
    checked: app.phase === 'up-to-date' || (!external && engine.availability === 'up-to-date'),
    engineOutdated,
    engineLatest: engine.latest,
    appAvailable: app.phase === 'available' || app.phase === 'downloading',
    appReady: app.phase === 'downloaded',
    appLatest: app.latest,
  }
}

/**
 * The head pill says the same thing as the hero, in words that cannot be
 * read as "everything is done" while optional items are open: required steps
 * left, else what is broken, else how many optional items remain.
 */
function summaryPillText(summary: SetupSummary): { text: string; tone: Tone } {
  if (summary.phase === 'offline') {
    return { text: t('settings.overview.pill.offline'), tone: 'danger' }
  }
  if (summary.requiredLeft > 0) {
    return {
      text:
        summary.requiredLeft === 1
          ? t('settings.overview.pill.requiredOne')
          : fill('settings.overview.pill.requiredMany', { n: summary.requiredLeft }),
      tone: 'warn',
    }
  }
  if (summary.toFix > 0) {
    return { text: fill('settings.overview.pill.toFix', { n: summary.toFix }), tone: 'warn' }
  }
  if (summary.optional > 0) {
    return { text: fill('settings.overview.pill.optional', { n: summary.optional }), tone: 'ok' }
  }
  return { text: t('settings.overview.pill.allSet'), tone: 'ok' }
}

function summaryPill(summary: SetupSummary) {
  const { text, tone } = summaryPillText(summary)
  return <Pill tone={tone}>{text}</Pill>
}

/**
 * Settings › Overview, the page Settings opens on: is the agent set up, and
 * what is the one next thing to do. A hero with the progress and the next
 * step, then the checklist split into what is required and what helps. Every
 * row's line is a fact from the gateway, and its button lands on the place
 * that changes it.
 */
export function OverviewPane() {
  const { connected, query, snapshot } = useConfigSnapshot()
  const gatewayStatus = useGateway((s) => s.status)
  const gatewayBusy = useGateway((s) => s.busy)
  const startGateway = useGateway((s) => s.start)
  const savedGateway = useSettings((s) => s.settings.gateway)
  const external = savedGateway.mode === 'external'
  const trading = (snapshot?.config as Record<string, unknown> | undefined)?.trading as
    { enabled?: boolean } | undefined
  const tradingOn = Boolean(trading) && trading?.enabled !== false
  const facts = useSideFacts(connected, tradingOn)
  const keyFacts = useKeyFacts(connected)
  const keys = keyFacts.failed ? null : keyFacts.ready ? keyFacts.ctx : undefined
  const updates = useUpdateFacts(external)

  const url = gatewayStatus.url ?? (savedGateway.host ? gatewayUrl(savedGateway) : null)
  const items = setupItems({
    connected,
    gateway: {
      state: gatewayStatus.state,
      external,
      url,
      error: gatewayStatus.error,
    },
    snapshot,
    ...facts,
    keys,
    updates,
  })
  const summary = summarizeSetup(items)
  const loading = connected && !snapshot && !query.isError

  function act(item: SetupItem) {
    if (item.jump.kind === 'start-gateway') void startGateway()
    else jumpTo(item.jump.section, item.jump.focus)
  }

  const required = items.filter((i) => i.group === 'required')
  const recommended = items.filter((i) => i.group === 'recommended')

  return (
    <>
      <Head
        title={t('settings.section.overview')}
        blurb={t('settings.overview.blurb')}
        action={
          loading ? (
            <Pill pulse>{t('settings.overview.pill.checking')}</Pill>
          ) : connected && query.isError ? null : (
            summaryPill(summary)
          )
        }
      />

      {snapshot?.writeBlocked && connected ? (
        <Notice tone="danger">{t('settings.writeBlocked')}</Notice>
      ) : snapshot?.pendingRestart && connected ? (
        <PendingRestartNotice />
      ) : null}

      {connected && query.isError ? (
        <Notice
          tone="danger"
          action={
            <Button disabled={query.isFetching} onClick={() => void query.refetch()}>
              {t('settings.overview.retry')}
            </Button>
          }
        >
          {t('settings.overview.loadFailed')}
        </Notice>
      ) : loading ? (
        <OverviewSkeleton />
      ) : (
        <>
          <Hero
            summary={summary}
            gateway={{
              state: gatewayStatus.state,
              external,
              host: hostOf(url),
              error: gatewayStatus.error,
            }}
            busy={gatewayBusy}
            onStep={act}
            onStartGateway={() => void startGateway()}
            onOpenGateway={() => jumpTo('gateway')}
          />
          <Checklist
            title={t('settings.overview.group.required')}
            blurb={t('settings.overview.group.required.blurb')}
            items={required}
            onAction={act}
            busy={gatewayBusy}
          >
            {!connected ? <p className="ovw-note">{t('settings.overview.offlineRest')}</p> : null}
          </Checklist>
          {recommended.length > 0 ? (
            <Checklist
              title={t('settings.overview.group.recommended')}
              blurb={t('settings.overview.group.recommended.blurb')}
              items={recommended}
              onAction={act}
            />
          ) : null}
        </>
      )}
    </>
  )
}

/** The page's shape while the first snapshot is on its way. */
function OverviewSkeleton() {
  return (
    <div className="ovw-skeleton" aria-busy="true" aria-label={t('settings.loading')}>
      <div className="ovw-skel ovw-skel--hero" />
      <div className="ovw-skel ovw-skel--card" />
      <div className="ovw-skel ovw-skel--card ovw-skel--tall" />
    </div>
  )
}
