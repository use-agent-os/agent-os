import { ArrowRight, CalendarCheck2, CalendarClock, HeartPulse, Plus, Sunrise } from 'lucide-react'
import { useId, useMemo, useState, type ReactNode } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { explainCron, jobKindLabel, type RawJob } from '@/views/cron/logic'
import { Button } from '@/web/components/ui/button'
import { cronTimesBetween } from './schedule'
import { Timeline, type LaneTone } from './Timeline'

// The page with no automations: a composed stage on the canvas, not a box.
// Heading, one line and the primary action; beside it a ghosted week showing
// what the templates would schedule; beneath, the templates as a gallery of
// cards that open the editor pre-filled.

export interface Template extends Partial<RawJob> {
  expression: string
  description: string
  icon: ReactNode
  tone: LaneTone
}

/** The quick starts; each seeds the create sheet. */
export function templates(): Template[] {
  return [
    {
      name: t('webPagesWork.templateStandupName'),
      expression: '0 9 * * 1-5',
      payloadKind: 'reminder',
      message: t('webPagesWork.templateStandupMessage'),
      description: t('webPagesWork.templateStandupHint'),
      icon: <Sunrise />,
      tone: 'warn',
    },
    {
      name: t('webPagesWork.templateHealthName'),
      expression: '0 * * * *',
      payloadKind: 'agent_turn',
      message: t('webPagesWork.templateHealthMessage'),
      description: t('webPagesWork.templateHealthHint'),
      icon: <HeartPulse />,
      tone: 'ok',
    },
    {
      name: t('webPagesWork.templateWrapName'),
      expression: '0 17 * * 5',
      payloadKind: 'agent_turn',
      message: t('webPagesWork.templateWrapMessage'),
      description: t('webPagesWork.templateWrapHint'),
      icon: <CalendarCheck2 />,
      tone: 'info',
    },
  ]
}

/** Strip the presentation fields before a template seeds the editor. */
function asJob({ expression, name, payloadKind, message }: Template): Partial<RawJob> {
  return { expression, name, payloadKind, message }
}

export function ZeroState({
  onCreate,
  disabledReason,
}: {
  onCreate: (template: Partial<RawJob> | null) => void
  /** Why creating is blocked right now (offline, scheduler off), if it is. */
  disabledReason: string | null
}) {
  const titleId = useId()
  const galleryId = useId()
  const reasonId = useId()
  const list = useMemo(() => templates(), [])
  // A week from the start of this hour: the preview does not tick.
  const [from] = useState(() => {
    const d = new Date()
    d.setMinutes(0, 0, 0)
    return d.getTime()
  })
  const lanes = useMemo(
    () =>
      list.map((tpl) => ({
        id: tpl.expression,
        name: String(tpl.name),
        tone: tpl.tone,
        times: cronTimesBetween(tpl.expression, '', from, from + 7 * 86_400_000, 200),
      })),
    [list, from],
  )

  return (
    <section data-slot="empty-state" aria-labelledby={titleId} className="am-zero">
      <div className="am-zero-hero">
        <div className="am-zero-copy anim-enter">
          <span aria-hidden className="am-zero-mark">
            <CalendarClock />
          </span>
          <h2 id={titleId} className="am-zero-title">
            {t('webPagesWork.automationsEmpty')}
          </h2>
          <p className="max-w-[34rem] text-pretty text-text-muted">
            {t('webPagesWork.automationsEmptyBody')}
          </p>
          <div className="mt-2 flex flex-col items-start gap-1.5">
            <Button
              variant="primary"
              size="lg"
              aria-describedby={disabledReason ? reasonId : undefined}
              onClick={() => onCreate(null)}
            >
              <Plus />
              {t('webPagesWork.automationsNew')}
            </Button>
            {disabledReason ? (
              <span id={reasonId} className="text-small text-text-muted">
                {disabledReason}
              </span>
            ) : null}
          </div>
        </div>
        <figure className="am-zero-preview anim-fade-in">
          <figcaption className="am-zero-caption">{t('webPagesWork.templatesPreview')}</figcaption>
          <Timeline lanes={lanes} from={from} span="week" animate ghost />
        </figure>
      </div>

      <section aria-labelledby={galleryId} className="flex flex-col gap-3">
        <h3 id={galleryId} className="am-eyebrow">
          {t('webPagesWork.templatesTitle')}
        </h3>
        <ul role="list" className="am-templates anim-stagger">
          {list.map((tpl) => (
            <li key={tpl.expression} className="min-w-0">
              <button
                type="button"
                className="am-template group"
                data-tone={tpl.tone}
                onClick={() => onCreate(asJob(tpl))}
              >
                <span className="flex items-start justify-between gap-3">
                  <span aria-hidden className="am-template-tile" data-tone={tpl.tone}>
                    {tpl.icon}
                  </span>
                  <span className="am-kind">{jobKindLabel(tpl)}</span>
                </span>
                <span className="am-template-name">{tpl.name}</span>
                <span className="am-template-when">
                  {explainCron(tpl.expression) || tpl.expression}
                </span>
                <span className="text-small text-pretty text-text-muted">{tpl.description}</span>
                <span className="am-template-foot">
                  <code className="font-mono text-small text-text-muted">{tpl.expression}</code>
                  <span className="am-template-use">
                    {t('webPagesWork.templateUse')}
                    <ArrowRight aria-hidden className="am-arrow" />
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>
    </section>
  )
}
