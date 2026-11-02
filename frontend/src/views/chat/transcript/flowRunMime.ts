// Chat transcript — the Image Flow cards' artifact mimes.
//
// Kept apart from flowRun.ts so the artifact classifier can recognise a flow
// card (a run, or an agent's change to a flow) without pulling the renderers
// into the chat bundle; they are loaded on demand by flowRunLazy.ts and share
// one placeholder.

import type { Artifact } from './artifacts'

/** The mime `flow_run` publishes a run card under. */
export const FLOW_RUN_ARTIFACT_MIME = 'application/vnd.agentos.flow-run+json'

/** The mime `flow_create` / `flow_update` publish a change card under. */
export const FLOW_CHANGE_ARTIFACT_MIME = 'application/vnd.agentos.flow-change+json'

/** True when the artifact should render as a flow card (run or change). */
export function isFlowRunArtifact(artifact: Artifact | null | undefined): boolean {
  if (!artifact || !artifact.mime) return false
  const mime = String(artifact.mime).toLowerCase().split(';')[0]?.trim()
  return mime === FLOW_RUN_ARTIFACT_MIME || mime === FLOW_CHANGE_ARTIFACT_MIME
}
