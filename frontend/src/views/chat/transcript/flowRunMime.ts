// Chat transcript — the Image Flow run card's artifact mime.
//
// Kept apart from flowRun.ts so the artifact classifier can recognise a run
// card without pulling the card renderer into the chat bundle; the renderer
// is loaded on demand by flowRunLazy.ts.

import type { Artifact } from './artifacts'

/** The mime `flow_run` publishes a run card under. */
export const FLOW_RUN_ARTIFACT_MIME = 'application/vnd.agentos.flow-run+json'

/** True when the artifact should render as a flow-run card. */
export function isFlowRunArtifact(artifact: Artifact | null | undefined): boolean {
  if (!artifact || !artifact.mime) return false
  return String(artifact.mime).toLowerCase().split(';')[0]?.trim() === FLOW_RUN_ARTIFACT_MIME
}
