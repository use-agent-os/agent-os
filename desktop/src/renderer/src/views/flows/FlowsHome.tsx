import './flows.css'
import { useQueryClient } from '@tanstack/react-query'
import {
  Eye,
  ImagePlus,
  MoreHorizontal,
  Plus,
  ShieldAlert,
  Sparkles,
  Trash2,
  Workflow,
  type LucideIcon,
} from 'lucide-react'
import { useId, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { ModalShell } from '@/components/ModalShell'
import { MenuItem, PopMenu } from '~/components/menu/PopMenu'
import { Button } from '~/components/ui/button'
import { t, type MessageKey } from '~/i18n'
import { shortAge } from '~/lib/relative-time'
import { useNow } from '~/lib/use-now'
import { useGateway } from '~/stores/gateway'
import {
  errorText,
  FLOW_ERRORS,
  FLOWS_KEYS,
  flowsErrorCode,
  useFlowList,
  useFlowsApi,
  useFlowsInvalidation,
  useFlowThumb,
} from '~/stores/flows'
import type { FlowList, FlowSummary, FlowTemplate } from './types'

const TEMPLATES: ReadonlyArray<{ id: FlowTemplate; icon: LucideIcon }> = [
  { id: 'blank', icon: Plus },
  { id: 'text_to_image', icon: Sparkles },
  { id: 'edit_image', icon: ImagePlus },
  { id: 'describe_remix', icon: Eye },
]

/** `#/flows`: your flows, newest first, and templates to start a new one. */
export function FlowsHome() {
  const gatewayState = useGateway((s) => s.status.state)
  if (gatewayState !== 'running') {
    return (
      <div className="flw-offline">
        <p className="flw-offline__title">
          {gatewayState === 'starting'
            ? t('chat.waitingGateway')
            : t(`gateway.state.${gatewayState}`)}
        </p>
        <p>{t('chat.gatewayDown')}</p>
      </div>
    )
  }
  return <ConnectedHome />
}

/** A failed write, said plainly; a gateway that refuses this window's writes says why. */
function refusal(err: unknown, fallback: MessageKey) {
  if (flowsErrorCode(err) === FLOW_ERRORS.operatorRequired) {
    toast.error(t('flows.readOnly.title'), {
      description: errorText(err) || t('flows.readOnly.body'),
    })
  } else {
    toast.error(t(fallback), { description: errorText(err) })
  }
}

function toMs(iso: string): number {
  const ms = Date.parse(iso)
  return Number.isFinite(ms) ? ms : 0
}

function ConnectedHome() {
  useFlowsInvalidation()
  const list = useFlowList()
  const api = useFlowsApi()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const now = useNow(30_000)
  const [creating, setCreating] = useState<FlowTemplate | null>(null)
  const [deleting, setDeleting] = useState<FlowSummary | null>(null)
  const [busyDelete, setBusyDelete] = useState(false)

  const create = async (template: FlowTemplate) => {
    if (creating) return
    setCreating(template)
    try {
      const name =
        template === 'blank'
          ? t('flows.home.newName')
          : t(`flows.template.${template}` as MessageKey)
      const res = await api.create(name, template)
      void queryClient.invalidateQueries({ queryKey: FLOWS_KEYS.list })
      void navigate(`/flows/${encodeURIComponent(res.flow.id)}`)
    } catch (err) {
      refusal(err, 'flows.home.createFailed')
    } finally {
      setCreating(null)
    }
  }

  const remove = async () => {
    if (!deleting) return
    setBusyDelete(true)
    try {
      await api.remove(deleting.id)
      queryClient.setQueryData<FlowList>(FLOWS_KEYS.list, (prev) =>
        prev ? { ...prev, flows: prev.flows.filter((f) => f.id !== deleting.id) } : prev,
      )
      void queryClient.invalidateQueries({ queryKey: FLOWS_KEYS.list })
      setDeleting(null)
    } catch (err) {
      refusal(err, 'flows.delete.failed')
    } finally {
      setBusyDelete(false)
    }
  }

  const flows = [...(list.data?.flows ?? [])].sort((a, b) => toMs(b.updated) - toMs(a.updated))
  // The gateway says up front when this window may not write (an agent's
  // connection): say so, and offer nothing that would only be refused.
  const readOnly = list.data && !list.data.writable ? (list.data.readOnlyReason ?? '') : null
  const blockedTitle = readOnly !== null ? t('flows.readOnly.title') : undefined

  return (
    <div className="flw-home" data-selectable>
      <header className="flw-home__head">
        <div className="flw-home__glyph" aria-hidden>
          <Workflow className="size-5" strokeWidth={1.75} />
        </div>
        <div className="flw-home__text">
          <h1>{t('flows.title')}</h1>
          <p>{t('flows.home.subtitle')}</p>
        </div>
        <Button
          variant="primary"
          disabled={creating !== null || readOnly !== null}
          title={blockedTitle}
          onClick={() => void create('blank')}
        >
          <Plus className="size-3.5" strokeWidth={2} aria-hidden />
          {creating === 'blank' ? t('flows.home.creating') : t('flows.home.new')}
        </Button>
      </header>

      {readOnly !== null ? (
        <div className="flw-banner flw-banner--inline" role="status">
          <ShieldAlert className="size-4 shrink-0" strokeWidth={1.75} aria-hidden />
          <div>
            <p className="flw-banner__title">{t('flows.readOnly.title')}</p>
            <p>{readOnly || t('flows.readOnly.body')}</p>
          </div>
        </div>
      ) : null}

      <section aria-labelledby="flw-templates">
        <h2 id="flw-templates" className="flw-home__section">
          {t('flows.home.templates')}
        </h2>
        <div className="flw-templates">
          {TEMPLATES.map(({ id, icon: Icon }) => (
            <button
              key={id}
              type="button"
              className="flw-template app-no-drag"
              disabled={creating !== null || readOnly !== null}
              title={blockedTitle}
              onClick={() => void create(id)}
            >
              <span className="flw-template__icon" aria-hidden>
                <Icon className="size-4" strokeWidth={1.75} />
              </span>
              <span className="flw-template__name">
                {creating === id
                  ? t('flows.home.creating')
                  : t(`flows.template.${id}` as MessageKey)}
              </span>
              <span className="flw-template__body">
                {t(`flows.template.${id}.body` as MessageKey)}
              </span>
            </button>
          ))}
        </div>
      </section>

      <section aria-labelledby="flw-yours">
        <h2 id="flw-yours" className="flw-home__section">
          {t('flows.home.yours')}
          {flows.length ? <span className="flw-home__count">{flows.length}</span> : null}
        </h2>
        {list.isLoading ? (
          <p className="flw-home__note">{t('flows.home.loading')}</p>
        ) : list.isError ? (
          <p className="flw-home__note">
            {t('flows.home.loadFailed')} · {errorText(list.error)}
          </p>
        ) : flows.length === 0 ? (
          <div className="flw-home__empty">
            <p>{t('flows.home.empty')}</p>
            <p className="flw-home__note">{t('flows.home.empty.hint')}</p>
          </div>
        ) : (
          <ul className="flw-cards">
            {flows.map((flow) => (
              <FlowCard
                key={flow.id}
                flow={flow}
                now={now}
                canDelete={readOnly === null}
                onDelete={() => setDeleting(flow)}
              />
            ))}
          </ul>
        )}
      </section>

      {deleting ? (
        <DeleteFlowConfirm
          name={deleting.name}
          busy={busyDelete}
          onCancel={() => setDeleting(null)}
          onConfirm={() => void remove()}
        />
      ) : null}
    </div>
  )
}

function FlowCard({
  flow,
  now,
  canDelete,
  onDelete,
}: {
  flow: FlowSummary
  now: number
  canDelete: boolean
  onDelete: () => void
}) {
  const thumb = useFlowThumb(flow.preview)
  const [menu, setMenu] = useState<DOMRect | null>(null)
  const moreRef = useRef<HTMLButtonElement>(null)
  const updated = toMs(flow.updated)
  const nodes = flow.node_count === 1 ? t('flows.home.nodes.one') : t('flows.home.nodes.many')
  return (
    <li className="flw-card" data-menu={menu ? true : undefined}>
      <Link
        to={`/flows/${encodeURIComponent(flow.id)}`}
        className="flw-card__link app-no-drag"
        aria-label={`${t('flows.home.open')} ${flow.name}`}
      >
        <span className="flw-card__preview" aria-hidden>
          {thumb.src ? (
            <img src={thumb.src} alt="" draggable={false} />
          ) : (
            <Workflow className="size-6 text-dim" strokeWidth={1.25} />
          )}
        </span>
        <span className="flw-card__name">{flow.name}</span>
        <span className="flw-card__meta">
          {flow.node_count} {nodes}
          {updated ? (
            <>
              {' · '}
              <span title={new Date(updated).toLocaleString()}>
                {t('flows.home.updated')} {shortAge(updated, now)}
              </span>
            </>
          ) : null}
        </span>
      </Link>
      <button
        ref={moreRef}
        type="button"
        className="flw-card__more flw-icon-btn app-no-drag"
        aria-label={t('flows.home.more')}
        aria-haspopup="menu"
        aria-expanded={Boolean(menu)}
        onClick={() => setMenu(moreRef.current?.getBoundingClientRect() ?? null)}
      >
        <MoreHorizontal className="size-4" strokeWidth={1.75} aria-hidden />
      </button>
      {menu ? (
        <PopMenu
          place={{ anchor: menu, align: 'end' }}
          onClose={() => setMenu(null)}
          triggerRef={moreRef}
        >
          <MenuItem
            icon={Trash2}
            tone="danger"
            label={t('flows.home.delete')}
            disabled={!canDelete}
            detail={canDelete ? undefined : t('flows.readOnly.title')}
            onSelect={onDelete}
          />
        </PopMenu>
      ) : null}
    </li>
  )
}

function DeleteFlowConfirm({
  name,
  busy,
  onCancel,
  onConfirm,
}: {
  name: string
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={onCancel}
      dismissible={!busy}
      overlayClassName="flw-alert__overlay"
      className="flw-alert"
    >
      <h2 id={titleId} className="flw-alert__title">
        {t('flows.delete.title')}
      </h2>
      <p id={bodyId} className="flw-alert__body">
        <strong>{name}</strong> — {t('flows.delete.body')}
      </p>
      <div className="flw-alert__actions">
        <Button disabled={busy} onClick={onCancel}>
          {t('flows.delete.cancel')}
        </Button>
        <Button variant="danger" disabled={busy} onClick={onConfirm}>
          {t('flows.delete.confirm')}
        </Button>
      </div>
    </ModalShell>
  )
}
