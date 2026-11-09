import { TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import { useBlocker } from 'react-router'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webSettings'
import { summariseDiffValue } from '@/views/config/logic'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/web/components/ui/alert-dialog'
import { Button } from '@/web/components/ui/button'
import { Dialog, DialogContent, DialogTitle } from '@/web/components/ui/dialog'
import type { ConfigEditor } from './config-editor'

/**
 * The sticky save bar: pending count, review (`path: old → new`), Discard and
 * Save; conflicts offer Reload / Overwrite; leaving with edits asks first.
 */
export function SaveBar({ editor }: { editor: ConfigEditor }) {
  const [review, setReview] = useState(false)
  const count = editor.changes.length
  const dirty = count > 0
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty && currentLocation.pathname !== nextLocation.pathname,
  )
  const saving = editor.state.kind === 'saving'

  return (
    <>
      {dirty || editor.state.kind === 'conflict' ? (
        <div
          role="region"
          aria-label={t('webSettings.reviewTitle')}
          className="sticky bottom-0 z-[var(--z-sticky)] mt-6 flex flex-wrap items-center gap-2 border-t border-border bg-surface-1/95 px-4 py-3 backdrop-blur"
        >
          <span className="font-medium" data-testid="unsaved-count">
            {tPlural('webSettings.unsaved', count)}
          </span>
          <Button size="sm" variant="link" onClick={() => setReview(true)} disabled={!dirty}>
            {t('webSettings.review')}
          </Button>
          <span className="flex-1" />
          {editor.state.kind === 'error' ? (
            <span role="alert" className="text-small text-danger">
              {t('webSettings.saveFailed', { message: editor.state.message })}
            </span>
          ) : null}
          {editor.state.kind === 'invalid' ? (
            <span role="alert" className="text-small text-danger">
              {t('webSettings.invalidFields')}
            </span>
          ) : null}
          <Button size="sm" variant="outline" onClick={editor.discard} disabled={saving}>
            {t('webSettings.discard')}
          </Button>
          <Button
            size="sm"
            variant="primary"
            onClick={() => void editor.save()}
            disabled={saving || editor.writeBlocked}
          >
            {saving ? t('webSettings.saving') : t('webSettings.save')}
          </Button>
          {editor.state.kind === 'error' ? (
            <Button size="sm" variant="ghost" onClick={() => void editor.save()}>
              {t('webCommon.retry')}
            </Button>
          ) : null}
          {editor.state.kind === 'conflict' ? (
            <div
              role="alert"
              className="flex w-full flex-wrap items-center gap-2 rounded-md border border-warn/50 bg-warn/10 px-3 py-2"
            >
              <TriangleAlert className="size-4 text-warn" aria-hidden data-status-icon />
              <span className="flex-1">
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
      ) : null}

      <Dialog open={review} onOpenChange={setReview}>
        <DialogContent>
          <DialogTitle>{t('webSettings.reviewTitle')}</DialogTitle>
          <ul className="flex flex-col gap-1 font-mono text-small" data-testid="pending-review">
            {editor.changes.map((c) => (
              <li key={c.path} className="break-all">
                {c.path}: {summariseDiffValue(c.old)} → {summariseDiffValue(c.next)}
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
                blocker.proceed?.()
              }}
            >
              {t('webSettings.leaveDiscard')}
            </Button>
            <Button
              variant="primary"
              onClick={async () => {
                if (await editor.save()) blocker.proceed?.()
                else blocker.reset?.()
              }}
            >
              {t('webSettings.leaveSave')}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
