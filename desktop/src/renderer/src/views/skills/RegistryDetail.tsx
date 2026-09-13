import { Check, ExternalLink, TriangleAlert } from 'lucide-react'
import { t as tw } from '@/i18n'
import {
  catLabel,
  registryKey,
  safeUrl,
  type InstallActionKind,
  type RegistryItem,
} from '@/views/skills/logic'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'

/**
 * The install control in its three states: already installed (a static
 * chip), force-armed after a dangerous scan verdict (a red button that says
 * so), or a plain install.
 */
export function InstallButton({
  action,
  busy,
  onInstall,
}: {
  action: InstallActionKind
  busy: boolean
  onInstall: (force: boolean) => void
}) {
  if (action === 'installed') {
    return (
      <span className="mac-chip sk-chip--lg" data-tone="ok">
        <Check className="size-3" strokeWidth={2.5} aria-hidden />
        {t('skills.action.installed')}
      </span>
    )
  }
  if (action === 'force') {
    return (
      <Button variant="danger" disabled={busy} onClick={() => onInstall(true)}>
        {busy ? null : <TriangleAlert className="size-3.5" strokeWidth={2} aria-hidden />}
        {busy ? t('skills.action.installing') : t('skills.action.forceInstall')}
      </Button>
    )
  }
  return (
    <Button variant="primary" disabled={busy} onClick={() => onInstall(false)}>
      {busy ? t('skills.action.installing') : t('skills.action.install')}
    </Button>
  )
}

/** The right pane for a catalog row: what it is, how to set it up, and Install. */
export function RegistryDetail({
  item,
  action,
  busy,
  onInstall,
  mark,
}: {
  item: RegistryItem
  action: InstallActionKind
  busy: boolean
  onInstall: (force: boolean) => void
  mark: React.ReactNode
}) {
  const homepage = safeUrl(item.homepage)
  const cat = item.category && item.category !== 'other' ? item.category : ''
  const trusted = item.trust_level === 'trusted'
  const demo = item.demo && item.demo.code ? item.demo : null
  const setup = Array.isArray(item.setup) ? item.setup.filter(Boolean) : []
  const open = (url: string) => void desktopApi().app.openExternal(url)

  return (
    <article className="sk-pane" aria-label={String(item.name || '')}>
      <header className="sk-pane__head">
        <div className="sk-pane__ident">
          {mark}
          <div className="sk-pane__title">
            <h1>{item.name}</h1>
            <div className="sk-pane__chips">
              <span className="mac-chip" data-tone={trusted ? 'ok' : 'warn'}>
                {item.trust_level || t('skills.fact.trust.community')}
              </span>
              {cat ? <span className="mac-chip">{catLabel(cat)}</span> : null}
              {item.source ? <span className="mac-chip sk-mono">{item.source}</span> : null}
            </div>
          </div>
        </div>
        <div className="sk-pane__actions">
          <InstallButton action={action} busy={busy} onInstall={onInstall} />
        </div>
      </header>

      {item.description ? (
        <p className="sk-pane__desc">{item.description}</p>
      ) : (
        <p className="sk-pane__desc text-dim">{tw('skills.descPending')}</p>
      )}

      {setup.length ? (
        <section className="sk-card">
          <span className="mac-label">{t('skills.section.setup')}</span>
          <ol className="sk-setup">
            {setup.map((step, i) => (
              <li key={i}>{step}</li>
            ))}
          </ol>
        </section>
      ) : null}

      {demo ? (
        <section className="sk-card">
          <span className="mac-label">
            {t('skills.section.demo')}
            {demo.title ? <span className="sk-mono sk-card__aside">{demo.title}</span> : null}
            {demo.language ? <span className="sk-mono sk-card__aside">{demo.language}</span> : null}
          </span>
          <pre className="sk-card__pre">
            <code>{demo.code}</code>
          </pre>
        </section>
      ) : null}

      <div className="sk-facts">
        {item.provider ? (
          <FactValue label={t('skills.fact.provider')}>{item.provider}</FactValue>
        ) : null}
        {item.source ? (
          <FactValue label={t('skills.fact.source')} mono>
            {item.source}
          </FactValue>
        ) : null}
        <div className="sk-fact sk-fact--wide">
          <span className="mac-label">{t('skills.fact.identifier')}</span>
          <span className="sk-fact__value" data-mono="true">
            {registryKey(item)}
          </span>
        </div>
        {homepage ? (
          <div className="sk-fact sk-fact--wide">
            <span className="mac-label">{t('skills.action.source')}</span>
            <button type="button" className="sk-link sk-fact__link" onClick={() => open(homepage)}>
              <span className="sk-fact__value">{homepage.replace(/^https?:\/\//, '')}</span>
              <ExternalLink className="size-3 shrink-0" strokeWidth={1.75} aria-hidden />
            </button>
          </div>
        ) : null}
      </div>
    </article>
  )
}

function FactValue({
  label,
  children,
  mono,
}: {
  label: string
  children: React.ReactNode
  mono?: boolean
}) {
  return (
    <div className="sk-fact">
      <span className="mac-label">{label}</span>
      <span className="sk-fact__value" data-mono={mono ? 'true' : undefined}>
        {children}
      </span>
    </div>
  )
}
