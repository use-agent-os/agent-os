import { ExternalLink, FolderOpen, TriangleAlert } from 'lucide-react'
import { t as tw } from '@/i18n'
import {
  layerHelp,
  layerLabel,
  safeUrl,
  skillAvailabilityLabel,
  skillAvailabilityTitle,
  skillAvailabilityTone,
  skillBucket,
  skillCanRemove,
  skillCanUpdate,
  skillDotTitle,
  skillStatus,
  type RawSkill,
  type SkillRequirementItem,
} from '@/views/skills/logic'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'
import {
  acquisitionSourceLabel,
  installedAtLabel,
  originLine,
  reqStatusLabel,
  reqStatusTone,
  requirementDetail,
  verdictTone,
} from './logic'
import { SkillMark } from './SkillMark'

const STATUS_TONE = { ready: 'ok', 'needs-setup': 'warn', disabled: undefined } as const

function Fact({
  label,
  children,
  mono,
  tone,
}: {
  label: string
  children: React.ReactNode
  mono?: boolean
  tone?: 'ok' | 'warn' | 'danger'
}) {
  return (
    <div className="sk-fact" data-tone={tone}>
      <span className="mac-label">{label}</span>
      <span className="sk-fact__value" data-mono={mono ? 'true' : undefined}>
        {children}
      </span>
    </div>
  )
}

function RequirementRow({ item }: { item: SkillRequirementItem }) {
  const status = item.status || 'not_declared'
  const detail = requirementDetail(item)
  return (
    <div className="sk-req">
      <span className="sk-req__name">{item.name || tw('skills.reqUnknown')}</span>
      <span className="mac-chip" data-tone={reqStatusTone(status)}>
        {reqStatusLabel(status)}
      </span>
      <span className="sk-req__detail">
        {detail.text}
        {detail.missing.map((m, i) => (
          <span key={m}>
            {i > 0 ? ', ' : ' '}
            <code>{m}</code>
          </span>
        ))}
      </span>
    </div>
  )
}

/**
 * The right pane for an installed skill: what it is, whether the agent can
 * actually reach it, what is missing on this Mac and the one-click fixes for
 * that, then the provenance facts. Every action the web dialog offers is
 * here — Use, Update, Remove, Install via …, Set <VAR> — plus Finder reveal,
 * which only a desktop can do.
 */
export function SkillDetail({
  skill,
  busyKeys,
  onUse,
  onUpdate,
  onRemove,
  onInstallDeps,
  onSetEnv,
}: {
  skill: RawSkill
  busyKeys: Set<string>
  onUse: () => void
  onUpdate: () => void
  onRemove: () => void
  onInstallDeps: (installId: string) => void
  onSetEnv: (name: string) => void
}) {
  const name = String(skill.name || '')
  const status = skillStatus(skill)
  const bucket = skillBucket(skill)
  const canUpdate = skillCanUpdate(skill)
  const canRemove = skillCanRemove(skill)
  const removeBlocked = skill.acquisition?.kind === 'hub' && !canRemove
  const availability = skillAvailabilityTone(skill)
  const withheldDetail = availability === 'not-offered' ? skillAvailabilityTitle(skill) : ''
  const homepage = safeUrl(skill.homepage)
  const updateBusy = busyKeys.has('update:' + name)
  const removeBusy = busyKeys.has('uninstall:' + name)
  const requirements = Array.isArray(skill.requirements?.items) ? skill.requirements.items : []
  // The missing list only applies while the skill actually needs setup.
  const missingBins = status === 'needs_setup' ? skill.missing_bins || [] : []
  const missingEnv = status === 'needs_setup' ? skill.missing_env || [] : []
  const installs = missingBins.length ? skill.install || [] : []
  const origin = originLine(skill)
  const acq = skill.acquisition
  const triggers = (skill.triggers || []).filter(Boolean)
  const open = (url: string) => void desktopApi().app.openExternal(url)

  return (
    <article className="sk-pane" aria-label={name}>
      <header className="sk-pane__head">
        <div className="sk-pane__ident">
          <SkillMark skill={skill} size="lg" />
          <div className="sk-pane__title">
            <h1>{name}</h1>
            <div className="sk-pane__chips">
              <span
                className="mac-chip"
                data-tone={STATUS_TONE[bucket]}
                title={skillDotTitle(skill)}
              >
                {bucket === 'ready'
                  ? t('skills.status.ready')
                  : bucket === 'disabled'
                    ? t('skills.status.disabled')
                    : t('skills.status.needsSetup')}
              </span>
              <span className="mac-chip" title={layerHelp(skill.layer)}>
                {layerLabel(skill.layer)}
              </span>
              {origin ? <span className="mac-chip">{origin}</span> : null}
              {availability === 'not-offered' ? (
                <span className="mac-chip" data-tone="warn" title={skillAvailabilityTitle(skill)}>
                  {skillAvailabilityLabel(skill)}
                </span>
              ) : availability === 'offered' ? (
                <span className="mac-chip" data-tone="ok" title={skillAvailabilityTitle(skill)}>
                  {t('skills.status.offered')}
                </span>
              ) : null}
            </div>
          </div>
        </div>
        <div className="sk-pane__actions">
          <Button variant="primary" onClick={onUse}>
            {t('skills.action.use')}
          </Button>
          {canUpdate ? (
            <Button disabled={updateBusy} onClick={onUpdate}>
              {updateBusy ? t('skills.action.updating') : t('skills.action.update')}
            </Button>
          ) : null}
          {canRemove ? (
            <Button variant="danger" disabled={removeBusy} onClick={onRemove}>
              {removeBusy ? t('skills.action.removing') : t('skills.action.remove')}
            </Button>
          ) : null}
        </div>
      </header>

      {skill.description ? <p className="sk-pane__desc">{skill.description}</p> : null}

      {withheldDetail ? (
        <section className="sk-card sk-card--warn">
          <span className="mac-label">{t('skills.section.availability')}</span>
          <p className="sk-card__text">{withheldDetail}</p>
        </section>
      ) : null}

      {requirements.length ? (
        <section className="sk-card">
          <span className="mac-label">{t('skills.section.requirements')}</span>
          <div className="sk-reqs">
            {requirements.map((item, i) => (
              <RequirementRow key={item.name || i} item={item} />
            ))}
          </div>
        </section>
      ) : null}

      {missingBins.length || missingEnv.length ? (
        <section className="sk-card sk-card--warn">
          <span className="mac-label">{t('skills.section.missing')}</span>
          <ul className="sk-missing">
            {missingBins.map((bin) => (
              <li key={`bin:${bin}`} className="sk-missing__row">
                <div className="sk-missing__info">
                  <code>{bin}</code> <span className="text-dim">{t('skills.missing.binary')}</span>
                </div>
              </li>
            ))}
            {missingEnv.map((env) => {
              const detail = (skill.missing_env_detail || []).find((d) => d.name === env)
              const url = safeUrl(detail?.url)
              return (
                <li key={`env:${env}`} className="sk-missing__row">
                  <div className="sk-missing__info">
                    <div>
                      <code>{env}</code> <span className="text-dim">{t('skills.missing.env')}</span>
                    </div>
                    {detail?.description || url ? (
                      <p className="sk-missing__desc">
                        {detail?.description}
                        {url ? (
                          <>
                            {detail?.description ? ' ' : null}
                            <button type="button" className="sk-link" onClick={() => open(url)}>
                              {t('skills.action.whereToGet')}
                            </button>
                          </>
                        ) : null}
                      </p>
                    ) : null}
                  </div>
                  <Button
                    aria-label={`${t('skills.env.title')} ${env}`}
                    onClick={() => onSetEnv(env)}
                  >
                    {t('skills.action.set')}
                  </Button>
                </li>
              )
            })}
          </ul>
        </section>
      ) : null}

      {installs.length ? (
        <section className="sk-card">
          <span className="mac-label">{t('skills.section.install')}</span>
          {installs.map((opt) => {
            const id = String(opt.id || '')
            const busy = busyKeys.has('deps:' + name + ':' + id)
            return (
              <div key={id} className="sk-install">
                <span>
                  {opt.label || `${t('skills.action.installVia')} ${opt.kind}`}
                  {(opt.bins || []).length ? (
                    <span className="text-dim"> ({(opt.bins || []).join(', ')})</span>
                  ) : null}
                </span>
                <Button disabled={busy || !id} onClick={() => onInstallDeps(id)}>
                  {busy
                    ? t('skills.action.installing')
                    : `${t('skills.action.installVia')} ${String(opt.kind || '')}`}
                </Button>
              </div>
            )
          })}
        </section>
      ) : null}

      <div className="sk-facts">
        <Fact label={t('skills.fact.source')}>{acquisitionSourceLabel(skill)}</Fact>
        <Fact label={t('skills.fact.layer')}>{layerLabel(skill.layer)}</Fact>
        {acq?.version ? (
          <Fact label={t('skills.fact.version')} mono>
            {acq.version}
          </Fact>
        ) : null}
        {installedAtLabel(acq?.installed_at) ? (
          <Fact label={t('skills.fact.installedAt')}>{installedAtLabel(acq?.installed_at)}</Fact>
        ) : null}
        {acq?.scan_verdict ? (
          <Fact label={t('skills.fact.scan')} tone={verdictTone(acq.scan_verdict)}>
            {acq.scan_verdict}
          </Fact>
        ) : null}
        {triggers.length ? (
          <Fact label={t('skills.fact.triggers')}>{triggers.join(', ')}</Fact>
        ) : null}
        {homepage ? (
          <div className="sk-fact">
            <span className="mac-label">{t('skills.action.homepage')}</span>
            <button type="button" className="sk-link sk-fact__link" onClick={() => open(homepage)}>
              <span className="sk-fact__value">{homepage.replace(/^https?:\/\//, '')}</span>
              <ExternalLink className="size-3 shrink-0" strokeWidth={1.75} aria-hidden />
            </button>
          </div>
        ) : null}
        {skill.file_path ? (
          <div className="sk-fact sk-fact--wide">
            <span className="mac-label">{t('skills.fact.path')}</span>
            <button
              type="button"
              className="sk-link sk-fact__link"
              title={t('skills.action.reveal')}
              onClick={() => void desktopApi().app.showItemInFolder(String(skill.file_path))}
            >
              <span className="sk-fact__value" data-mono="true">
                {skill.file_path}
              </span>
              <FolderOpen className="size-3 shrink-0" strokeWidth={1.75} aria-hidden />
            </button>
          </div>
        ) : null}
      </div>

      {removeBlocked ? (
        <p className="sk-pane__note">
          <TriangleAlert className="size-3.5 shrink-0" strokeWidth={1.75} aria-hidden />
          {t('skills.removeBlocked')}
        </p>
      ) : null}
    </article>
  )
}
