import { ArrowRight, LoaderCircle, PartyPopper, ServerOff } from 'lucide-react'
import type { GatewayState } from '@shared/gateway'
import { Button } from '~/components/ui/button'
import { t, type MessageKey } from '~/i18n'
import { heroCopy, midSentence, type HeroCopy } from './hero-copy'
import type { SetupItem, SetupSummary } from './setup-items'

function fill(key: MessageKey, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce(
    (text, [name, value]) => text.replace(`{${name}}`, String(value)),
    t(key),
  )
}

/** The figure in the hero: how much of the checklist is done, as a ring. */
export function ProgressRing({
  done,
  total,
  tone,
}: {
  done: number
  total: number
  tone: 'ok' | 'warn' | 'primary'
}) {
  const r = 27
  const length = 2 * Math.PI * r
  const ratio = total > 0 ? Math.min(1, done / total) : 0
  return (
    <div
      className="ovw-ring"
      data-tone={tone}
      role="progressbar"
      aria-label={t('settings.overview.progress.label')}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={done}
      aria-valuetext={fill('settings.overview.progress', { done, total })}
    >
      <svg viewBox="0 0 64 64" aria-hidden>
        <circle className="ovw-ring__track" cx="32" cy="32" r={r} />
        <circle
          className="ovw-ring__fill"
          cx="32"
          cy="32"
          r={r}
          strokeDasharray={length}
          strokeDashoffset={length * (1 - ratio)}
        />
      </svg>
      <span className="ovw-ring__text" aria-hidden>
        <b>{done}</b>
        <small>/{total}</small>
      </span>
    </div>
  )
}

function nextLabel(item: SetupItem): string {
  if (item.state === 'attention') {
    // Mid-sentence: "Fix web search", not "Fix Web search".
    return fill('settings.overview.fix', { title: midSentence(item.title) })
  }
  return t(`settings.overview.next.${item.id}`)
}

export function Hero({
  summary,
  gateway,
  busy,
  onStep,
  onStartGateway,
  onOpenGateway,
}: {
  summary: SetupSummary
  gateway: { state: GatewayState; external: boolean; host: string; error: string | null }
  busy: boolean
  onStep: (item: SetupItem) => void
  onStartGateway: () => void
  onOpenGateway: () => void
}) {
  const next = summary.next

  if (summary.phase === 'offline') {
    const starting = gateway.state === 'starting' || gateway.state === 'stopping'
    const failed = gateway.state === 'error' && !gateway.external
    const copy: HeroCopy = starting
      ? {
          title: t('settings.overview.hero.starting.title'),
          body: t('settings.overview.hero.starting.body'),
        }
      : gateway.external
        ? {
            title: t('settings.overview.hero.unreachable.title'),
            body: fill('settings.overview.hero.unreachable.body', { url: gateway.host }),
          }
        : failed
          ? {
              title: t('settings.overview.hero.failed.title'),
              body: gateway.error ?? '',
            }
          : {
              title: t('settings.overview.hero.offline.title'),
              body: t('settings.overview.hero.offline.body'),
            }
    return (
      <section className="ovw-hero" data-phase="offline" aria-label={copy.title}>
        <div className="ovw-hero__tile" data-tone={starting ? 'warn' : 'danger'} aria-hidden>
          {starting ? (
            <LoaderCircle className="stg-spin" strokeWidth={1.75} />
          ) : (
            <ServerOff strokeWidth={1.75} />
          )}
        </div>
        <div className="ovw-hero__text">
          <h2>{copy.title}</h2>
          {copy.body ? <p className={failed ? 'ovw-hero__error' : undefined}>{copy.body}</p> : null}
        </div>
        <div className="ovw-hero__actions">
          {starting ? null : gateway.external ? (
            <>
              <Button onClick={onOpenGateway}>{t('settings.overview.openGateway')}</Button>
              <Button variant="primary" disabled={busy} onClick={onStartGateway}>
                {t('settings.overview.tryAgain')}
              </Button>
            </>
          ) : (
            <>
              {failed ? (
                <Button onClick={onOpenGateway}>{t('settings.overview.openGateway')}</Button>
              ) : null}
              <Button variant="primary" disabled={busy} onClick={onStartGateway}>
                {busy ? (
                  <LoaderCircle className="stg-spin size-3.5" strokeWidth={1.75} aria-hidden />
                ) : null}
                {failed ? t('settings.overview.tryAgain') : t('settings.overview.startGateway')}
              </Button>
            </>
          )}
        </div>
      </section>
    )
  }

  const copy = heroCopy(summary)
  const tone =
    summary.phase === 'complete'
      ? 'ok'
      : summary.phase === 'blocked' || summary.phase === 'attention'
        ? 'warn'
        : 'primary'

  return (
    <section className="ovw-hero" data-phase={summary.phase} aria-label={copy.title}>
      {summary.phase === 'complete' ? (
        <div className="ovw-hero__tile" data-tone="ok" aria-hidden>
          <PartyPopper strokeWidth={1.75} />
        </div>
      ) : (
        <ProgressRing done={summary.done} total={summary.total} tone={tone} />
      )}
      <div className="ovw-hero__text">
        <span className="ovw-hero__kicker">
          {fill('settings.overview.progress', { done: summary.done, total: summary.total })}
        </span>
        <h2>{copy.title}</h2>
        <p>{copy.body}</p>
      </div>
      {next && summary.phase !== 'complete' ? (
        <div className="ovw-hero__actions">
          <Button variant="primary" disabled={busy} onClick={() => onStep(next)}>
            {nextLabel(next)}
            <ArrowRight className="size-3.5" strokeWidth={2} aria-hidden />
          </Button>
        </div>
      ) : null}
    </section>
  )
}
