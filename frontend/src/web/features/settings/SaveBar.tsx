import { Check, Loader2, RefreshCw, RotateCw, TriangleAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useBlocker } from 'react-router'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webSetConfig'
import '@/i18n/en/webSettings'
import { isSensitiveKey, summariseDiffValue } from '@/views/config/logic'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/web/components/ui/alert-dialog'
import { StatusDot } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Dialog, DialogContent, DialogTitle } from '@/web/components/ui/dialog'
import type { ConfigEditor } from './config-editor'
import { Notice } from './ui'

const SAVED_MS = 1800

/**
 * True for a moment after a save cleared every pending edit: the bar gives
 * way to a small "Saved" confirmation. Tracked during render (saving→clean).
 */
function useSavedFlash(saving: boolean, dirty: boolean): boolean {
  const [track, setTrack] = useState({ saving, dirty, pulse: 0 })
  if (track.saving !== saving || track.dirty !== dirty) {
    const pulse = track.saving && !saving && !dirty ? track.pulse + 1 : track.pulse
    setTrack({ saving, dirty, pulse })
  }
  const [hidden, setHidden] = useState(0)
  useEffect(() => {
    if (!track.pulse) return
    const id = window.setTimeout(() => setHidden(track.pulse), SAVED_MS)
    return () => window.clearTimeout(id)
  }, [track.pulse])
  return track.pulse > hidden && !dirty
}

/**
 * The sticky save bar: pending count, review (`path: old → new`), Discard and
 * Save; conflicts offer Reload / Overwrite; leaving with edits asks first.
 */
/**
 * The config file changed on disk and the gateway refuses writes until it is
 * reloaded: say so, and offer to load what the gateway has now.
 */
export function WriteBlockedNotice({ onReload }: { onReload(): void }) {
  return (
    <Notice
      role="alert"
      tone="warn"
      className="mb-4"
      actions={
        <Button size="sm" variant="outline" onClick={onReload}>
          <RefreshCw />
          {t('webSettings.reload')}
        </Button>
      }
    >
      {t('webSettings.writeBlocked')}
    </Notice>
  )
}

function diffValue(path: string, value: unknown): string {
  // A typed secret must not appear on screen just because it is pending.
  if (isSensitiveKey(path) && typeof value === 'string' && value !== '') {
    return t('webSettings.maskedValue')
  }
  return summariseDiffValue(value)
}

export function SaveBar({
  editor,
  other,
}: {
  editor: ConfigEditor
  /** Unsaved work this bar does not save (Advanced's YAML draft): still guards leaving. */
  other?: { onDiscard(): void }
}) {
  const [review, setReview] = useState(false)
  const count = editor.changes.length
  const invalid = editor.invalid.length > 0
  const dirty = count > 0 || invalid
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      (dirty || Boolean(other)) && currentLocation.pathname !== nextLocation.pathname,
  )
  const saving = editor.state.kind === 'saving'
  const saved = useSavedFlash(saving, dirty)
  const kind = editor.state.kind
  const tone =
    kind === 'conflict'
      ? 'conflict'
      : kind === 'error' || kind === 'invalid' || invalid
        ? 'error'
        : undefined

  return (
    <>
      {dirty || kind === 'conflict' ? (
        <div
          role="region"
          aria-label={t('webSettings.reviewTitle')}
          data-tone={tone}
          className="set-savebar sticky bottom-4 z-[var(--z-sticky)] mt-8"
        >
          <div className="set-savebar-inner flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5">
            <span className="flex items-center gap-2.5">
              <StatusDot
                tone={tone === 'error' ? 'danger' : tone === 'conflict' ? 'warn' : 'accent'}
                className="size-2"
              />
              <span className="font-medium tabular-nums" data-testid="unsaved-count">
                {tPlural('webSettings.unsaved', count)}
              </span>
            </span>
            <Button size="sm" variant="link" onClick={() => setReview(true)} disabled={count === 0}>
              {t('webSettings.review')}
            </Button>
            <span className="flex-1" />
            {kind === 'error' ? (
              <span role="alert" className="text-small text-danger">
                {t('webSettings.saveFailed', { message: editor.state.message })}
              </span>
            ) : null}
            {invalid ? (
              <span role="alert" className="text-small text-danger">
                {t('webSettings.fixJson', { paths: editor.invalid.join(', ') })}
              </span>
            ) : null}
            {kind === 'invalid' ? (
              <span role="alert" className="text-small text-danger">
                {t('webSettings.invalidFields')}
              </span>
            ) : null}
            <Button size="sm" variant="ghost" onClick={editor.discard} disabled={saving}>
              {t('webSettings.discard')}
            </Button>
            {kind === 'error' ? (
              <Button size="sm" variant="outline" onClick={() => void editor.save()}>
                <RotateCw />
                {t('webCommon.retry')}
              </Button>
            ) : null}
            <Button
              size="sm"
              variant="primary"
              className="min-w-20"
              onClick={() => void editor.save()}
              disabled={saving || invalid || editor.writeBlocked}
            >
              {saving ? <Loader2 className="animate-spin" /> : <Check />}
              {saving ? t('webSettings.saving') : t('webSettings.save')}
            </Button>
            {kind === 'conflict' ? (
              <div
                role="alert"
                className="anim-fade-in flex w-full flex-wrap items-center gap-2 rounded-control border border-warn/30 bg-warn/10 px-3 py-2"
              >
                <TriangleAlert className="size-4 text-warn" aria-hidden data-status-icon />
                <span className="flex-1 text-small">
                  <strong>{t('webSettings.conflictTitle')}</strong> {t('webSettings.conflictBody')}
                </span>
                <Button size="sm" variant="outline" onClick={() => void editor.reload()}>
                  {t('webSettings.conflictReload')}
                </Button>
                <Button size="sm" variant="primary" onClick={() => void editor.overwrite()}>
                  {t('webSettings.conflictOverwrite')}
                </Button>
              </div>
            ) : null}
          </div>
        </div>
      ) : saved ? (
        <div
          role="status"
          className="set-saved sticky bottom-4 z-[var(--z-sticky)] mt-8 flex justify-center"
        >
          <span className="inline-flex items-center gap-2 rounded-full border border-border-strong bg-surface-overlay px-3.5 py-1.5 text-small font-medium shadow-overlay">
            <span className="grid size-4 place-items-center rounded-full bg-ok/15 text-ok">
              <Check className="size-3" aria-hidden data-status-icon />
            </span>
            {t('webSetConfig.applied')}
          </span>
        </div>
      ) : null}

      <Dialog open={review} onOpenChange={setReview}>
        <DialogContent>
          <DialogTitle>{t('webSettings.reviewTitle')}</DialogTitle>
          <ul className="set-grid flex flex-col font-mono text-small" data-testid="pending-review">
            {editor.changes.map((c) => (
              <li key={c.path} className="px-3 py-2 break-all">
                <span className="text-text">{c.path}</span>
                {': '}
                <span className="text-text-muted line-through decoration-text-muted/50">
                  {diffValue(c.path, c.old)}
                </span>
                {' → '}
                <span className="text-accent">{diffValue(c.path, c.next)}</span>
              </li>
            ))}
          </ul>
        </DialogContent>
      </Dialog>

      <AlertDialog open={blocker.state === 'blocked'}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('webSettings.leaveTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('webSettings.leaveBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => blocker.reset?.()}>
              {t('webSettings.leaveKeep')}
            </AlertDialogCancel>
            <Button
              variant="outline"
              onClick={() => {
                editor.discard()
                other?.onDiscard()
                blocker.proceed?.()
              }}
            >
              {t('webSettings.leaveDiscard')}
            </Button>
            {other ? null : (
              <Button
                variant="primary"
                onClick={async () => {
                  if (await editor.save()) blocker.proceed?.()
                  else blocker.reset?.()
                }}
              >
                {t('webSettings.leaveSave')}
              </Button>
            )}
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
