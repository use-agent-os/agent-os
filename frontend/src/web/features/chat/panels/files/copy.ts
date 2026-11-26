import { t } from '@/i18n'
import '@/i18n/en/webPanel'
import { failureKind, type FailureKind } from '@/lib/workspace-files'

// The Files tab's words for a refused or failed read, by kind.

const TITLES = {
  outdated: 'webPanel.filesErrorOutdatedTitle',
  denied: 'webPanel.filesErrorDeniedTitle',
  unsafe: 'webPanel.filesErrorUnsafeTitle',
  not_found: 'webPanel.filesErrorNotFoundTitle',
  too_large: 'webPanel.filesErrorTooLargeTitle',
  unsupported: 'webPanel.filesErrorUnsupportedTitle',
  failed: 'webPanel.filesErrorFailedTitle',
} as const

const BODIES = {
  outdated: 'webPanel.filesErrorOutdatedBody',
  denied: 'webPanel.filesErrorDeniedBody',
  unsafe: 'webPanel.filesErrorUnsafeBody',
  not_found: 'webPanel.filesErrorNotFoundBody',
  too_large: 'webPanel.filesErrorTooLargeBody',
  unsupported: 'webPanel.filesErrorUnsupportedBody',
} as const

export interface FailureCopy {
  kind: FailureKind
  title: string
  body: string
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** Title and body for an RPC rejection from `workspace.files.*`. */
export function failureCopy(error: unknown): FailureCopy {
  const kind = failureKind(error)
  return {
    kind,
    title: t(TITLES[kind]),
    body: kind === 'failed' ? message(error) : t(BODIES[kind]),
  }
}
