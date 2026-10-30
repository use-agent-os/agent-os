// Chat transcript — on-demand loader for Image Flow run cards.
//
// Most transcripts never show a flow-run card, so the renderer (flowRun.ts)
// stays out of the chat bundle: it is imported the first time a mount pass
// finds a `[data-flowrun-src]` placeholder. Until then events and reconnects
// are no-ops — there is no card to refresh, and a card re-reads its run when
// it mounts.

import type { FlowRunMounter, FlowRunMounterDeps } from './flowRun'

export type LazyFlowRunMounter = Pick<
  FlowRunMounter,
  'mountFlowRuns' | 'runEvent' | 'reconnected' | 'destroyAll'
>

export function createLazyFlowRunMounter(
  deps: FlowRunMounterDeps,
  load: () => Promise<typeof import('./flowRun')> = () => import('./flowRun'),
): LazyFlowRunMounter {
  let real: FlowRunMounter | null = null
  let loading: Promise<FlowRunMounter | null> | null = null
  /** Roots handed to mountFlowRuns while the module was loading. */
  const waiting = new Set<HTMLElement>()

  function ensure(): Promise<FlowRunMounter | null> {
    if (!loading) {
      loading = load().then(
        (mod) => {
          real = mod.createFlowRunMounter(deps)
          return real
        },
        (err: unknown) => {
          // A failed chunk load may succeed later (e.g. after a reconnect).
          loading = null
          deps.diag?.('flowrun.load_failed', { error: String(err) })
          return null
        },
      )
    }
    return loading
  }

  function mountFlowRuns(root: HTMLElement | null | undefined): void {
    if (!root) return
    if (real) {
      real.mountFlowRuns(root)
      return
    }
    if (!root.querySelector('[data-flowrun-src]')) return
    waiting.add(root)
    void ensure().then((mounter) => {
      if (!mounter) return
      const roots = [...waiting]
      waiting.clear()
      roots.forEach((r) => mounter.mountFlowRuns(r))
    })
  }

  return {
    mountFlowRuns,
    runEvent: (payload) => real?.runEvent(payload),
    reconnected: () => real?.reconnected(),
    destroyAll: () => {
      waiting.clear()
      real?.destroyAll()
    },
  }
}
