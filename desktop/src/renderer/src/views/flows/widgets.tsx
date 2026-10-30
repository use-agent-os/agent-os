import { ChevronDown, ChevronLeft, ChevronRight, ImagePlus, Lock, Shuffle } from 'lucide-react'
import { useRef, useState, type ReactNode } from 'react'
import { Switch } from '~/components/ui/switch'
import { t } from '~/i18n'
import { cn } from '~/lib/utils'
import { useFlowThumb } from '~/stores/flows'
import { useEditorEnv } from './editor-context'
import { UPLOAD_ACCEPT } from './files'
import { clampNumber, resolveOptions } from './schema-logic'
import type { ImageRef, InputSpec, ModelWire, WidgetKind } from './types'

/**
 * One small component per widget kind (contract §3). A widget gets its spec,
 * the value to show and a way to change it; the node decides visibility,
 * links and errors. `nodrag` / `nowheel` keep the canvas from panning or
 * dragging the node while someone types or scrolls inside a control.
 */

export interface WidgetProps {
  nodeId: string
  spec: InputSpec
  value: unknown
  model: ModelWire | null
  /** Models registry default, for the "Auto · …" label. */
  defaultModel: ModelWire | null
  invalid: boolean
  /** Coalesce: part of one burst of typing (one undo step). */
  onChange: (value: unknown, coalesce?: boolean) => void
}

export function widgetLabel(spec: InputSpec): string {
  return spec.name.replace(/_/g, ' ')
}

function Row({
  spec,
  invalid,
  className,
  children,
}: {
  spec: InputSpec
  invalid: boolean
  className?: string
  children: ReactNode
}) {
  return (
    <div
      className={cn('flw-w', spec.advanced && 'is-advanced', invalid && 'is-invalid', className)}
      title={spec.tooltip}
    >
      {children}
    </div>
  )
}

function TextWidget({ spec, value, invalid, onChange }: WidgetProps) {
  return (
    <Row spec={spec} invalid={invalid}>
      <span className="flw-w__label">{widgetLabel(spec)}</span>
      <input
        className="flw-w__input nodrag"
        value={typeof value === 'string' ? value : String(value ?? '')}
        placeholder={spec.placeholder}
        spellCheck={false}
        aria-label={widgetLabel(spec)}
        onChange={(e) => onChange(e.target.value, true)}
      />
    </Row>
  )
}

function TextareaWidget({ spec, value, invalid, onChange }: WidgetProps) {
  return (
    <Row spec={spec} invalid={invalid} className="flw-w--area">
      <span className="flw-w__label">{widgetLabel(spec)}</span>
      <textarea
        className="flw-w__area nodrag nowheel"
        value={typeof value === 'string' ? value : String(value ?? '')}
        placeholder={spec.placeholder}
        aria-label={widgetLabel(spec)}
        rows={2}
        onChange={(e) => onChange(e.target.value, true)}
      />
    </Row>
  )
}

function NumberWidget({ spec, value, invalid, onChange }: WidgetProps) {
  const current = typeof value === 'number' ? value : Number(value ?? spec.min ?? 0)
  const [draft, setDraft] = useState<string | null>(null)
  const step = spec.step ?? 1
  const commit = (raw: string) => {
    setDraft(null)
    const n = Number(raw)
    if (raw.trim() === '' || !Number.isFinite(n)) return
    const next = clampNumber(spec, n)
    if (next !== current) onChange(next)
  }
  return (
    <Row spec={spec} invalid={invalid} className="flw-w--number">
      <button
        type="button"
        className="flw-w__step nodrag"
        aria-label={`${t('flows.widget.decrease')} ${widgetLabel(spec)}`}
        disabled={spec.min !== undefined && current <= spec.min}
        onClick={() => onChange(clampNumber(spec, current - step))}
      >
        <ChevronLeft className="size-3" strokeWidth={2} aria-hidden />
      </button>
      <span className="flw-w__label">{widgetLabel(spec)}</span>
      <input
        className="flw-w__num nodrag"
        inputMode="decimal"
        aria-label={widgetLabel(spec)}
        value={draft ?? String(current)}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={(e) => commit(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commit((e.target as HTMLInputElement).value)
          if (e.key === 'Escape') setDraft(null)
        }}
      />
      <button
        type="button"
        className="flw-w__step nodrag"
        aria-label={`${t('flows.widget.increase')} ${widgetLabel(spec)}`}
        disabled={spec.max !== undefined && current >= spec.max}
        onClick={() => onChange(clampNumber(spec, current + step))}
      >
        <ChevronRight className="size-3" strokeWidth={2} aria-hidden />
      </button>
    </Row>
  )
}

function ComboWidget({ spec, value, model, invalid, onChange }: WidgetProps) {
  const options = resolveOptions(spec, model)
  const current = String(value ?? '')
  return (
    <Row spec={spec} invalid={invalid} className="flw-w--combo">
      <span className="flw-w__label">{widgetLabel(spec)}</span>
      <span className="flw-w__value">{current || '—'}</span>
      <ChevronDown className="flw-w__chev" strokeWidth={2} aria-hidden />
      <select
        className="flw-w__select nodrag"
        aria-label={widgetLabel(spec)}
        value={current}
        disabled={options.length === 0}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </Row>
  )
}

function ToggleWidget({ spec, value, invalid, onChange }: WidgetProps) {
  return (
    <Row spec={spec} invalid={invalid}>
      <span className="flw-w__label">{widgetLabel(spec)}</span>
      <Switch
        className="ml-auto nodrag"
        checked={value === true}
        aria-label={widgetLabel(spec)}
        onCheckedChange={(next) => onChange(next)}
      />
    </Row>
  )
}

function ModelWidget({ nodeId, spec, value, model, defaultModel, invalid }: WidgetProps) {
  const env = useEditorEnv()
  const ref = useRef<HTMLButtonElement>(null)
  const auto = value === 'auto' || value == null || value === ''
  const label = auto
    ? defaultModel
      ? t('flows.model.autoDefault').replace('{name}', defaultModel.display)
      : t('flows.model.autoNone')
    : (model?.display ?? String(value))
  const blocked = (auto ? !defaultModel : !model) || (model ? !model.available : false)
  return (
    <Row spec={spec} invalid={invalid || blocked} className="flw-w--model">
      <button
        ref={ref}
        type="button"
        className="flw-w__hit nodrag"
        aria-haspopup="dialog"
        aria-label={`${widgetLabel(spec)}: ${label}`}
        title={model && !model.available ? (model.unavailable_reason ?? undefined) : undefined}
        onClick={() => {
          const rect = ref.current?.getBoundingClientRect()
          if (rect) env.openModelPicker(nodeId, rect)
        }}
      >
        <span className="flw-w__label">{widgetLabel(spec)}</span>
        <span className="flw-w__value">{label}</span>
        <ChevronDown className="flw-w__chev" strokeWidth={2} aria-hidden />
      </button>
    </Row>
  )
}

function VariationWidget({ spec, value, invalid, onChange }: WidgetProps) {
  const keep = value === 'keep'
  return (
    <Row spec={spec} invalid={invalid} className={cn('flw-w--variation', keep && 'is-locked')}>
      <span className="flw-w__label" title={t('flows.widget.variation.hint')}>
        {widgetLabel(spec)}
      </span>
      <span className="flw-seg nodrag" role="radiogroup" aria-label={widgetLabel(spec)}>
        <button
          type="button"
          role="radio"
          aria-checked={!keep}
          className="flw-seg__opt"
          onClick={() => onChange('new')}
        >
          <Shuffle className="size-3" strokeWidth={2} aria-hidden />
          {t('flows.widget.variation.new')}
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={keep}
          className="flw-seg__opt"
          onClick={() => onChange('keep')}
        >
          <Lock className="size-3" strokeWidth={2} aria-hidden />
          {t('flows.widget.variation.keep')}
        </button>
      </span>
    </Row>
  )
}

function isImageRef(v: unknown): v is ImageRef {
  return Boolean(v && typeof v === 'object' && typeof (v as ImageRef).sha256 === 'string')
}

function ImageUploadWidget({ nodeId, spec, value, invalid }: WidgetProps) {
  const env = useEditorEnv()
  const input = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const image = isImageRef(value) ? value : null
  const thumb = useFlowThumb(image)
  return (
    <Row spec={spec} invalid={invalid} className="flw-w--upload">
      {image ? (
        <button
          type="button"
          className="flw-preview nodrag"
          aria-label={t('flows.node.openImage')}
          onClick={() => env.openImages([image], 0)}
          style={{ aspectRatio: `${image.width || 3} / ${image.height || 2}` }}
        >
          {thumb.src ? <img src={thumb.src} alt="" draggable={false} /> : null}
          <span className="flw-preview__cap">
            {image.width}×{image.height}
          </span>
        </button>
      ) : null}
      <button
        type="button"
        className="flw-w__hit flw-w__upload nodrag"
        disabled={busy}
        onClick={() => input.current?.click()}
      >
        <ImagePlus className="size-3.5" strokeWidth={1.75} aria-hidden />
        <span>
          {busy
            ? t('flows.widget.uploading')
            : image
              ? t('flows.widget.replace')
              : t('flows.widget.upload')}
        </span>
      </button>
      <input
        ref={input}
        type="file"
        accept={UPLOAD_ACCEPT}
        hidden
        onChange={async (e) => {
          const file = e.target.files?.[0]
          e.target.value = ''
          if (!file) return
          setBusy(true)
          try {
            await env.uploadImage(nodeId, spec.name, file)
          } finally {
            setBusy(false)
          }
        }}
      />
    </Row>
  )
}

export const WIDGETS: Record<WidgetKind, (props: WidgetProps) => ReactNode> = {
  text: TextWidget,
  textarea: TextareaWidget,
  number: NumberWidget,
  combo: ComboWidget,
  toggle: ToggleWidget,
  model: ModelWidget,
  variation: VariationWidget,
  image_upload: ImageUploadWidget,
}

/** A widget whose input is fed by a link: shown, disabled, naming its source. */
export function LinkedWidget({
  spec,
  source,
  text,
  invalid,
}: {
  spec: InputSpec
  source: string
  text: string | null
  invalid: boolean
}) {
  return (
    <Row
      spec={spec}
      invalid={invalid}
      className={cn('is-linked', spec.widget === 'textarea' && 'flw-w--area')}
    >
      <span className="flw-w__label">
        {widgetLabel(spec)} ← {source}
      </span>
      {spec.widget === 'textarea' ? (
        <span className="flw-w__linked-text">{text ?? t('flows.widget.linked')}</span>
      ) : (
        <span className="flw-w__value">{text ?? t('flows.widget.linked')}</span>
      )}
    </Row>
  )
}
