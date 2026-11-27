type QueryKey = readonly unknown[]

/**
 * The part of a `QueryClient` this needs, spelled out: the desktop imports
 * this module with its own copy of the query library, and two copies of the
 * class are not the same type.
 */
export interface InvalidatingClient {
  getQueryCache(): {
    findAll(filters: { queryKey: QueryKey; fetchStatus: 'fetching' }): Array<{
      queryKey: QueryKey
      queryHash: string
      promise?: Promise<unknown>
    }>
  }
  invalidateQueries(
    filters: { queryKey: QueryKey; exact?: boolean },
    options: { cancelRefetch: boolean },
  ): Promise<void>
}

/** What `createEventInvalidator` hands back. */
export interface EventInvalidator {
  /** A gateway event said the data under the key is stale. */
  notify: () => void
  /** Stop: no round and no owed re-read runs after this. */
  dispose: () => void
}

/**
 * Refetches everything under `queryKey` when the gateway says it changed,
 * with at most ONE request per query on the wire.
 *
 * A plain `invalidateQueries` drops the read in flight and sends another.
 * The socket cannot take a request back, so the dropped one stays in the
 * gateway's start queue; an engine that emits events faster than it answers
 * reads (a sync pass on many wallets) then gets a full round of new requests
 * per debounce, the connection's queue fills, and every request on it is
 * refused `QUEUE_FULL`, the status reads included.
 *
 * So a read already on its way is left alone, and is asked once more when it
 * lands: it may have started before the event and carry the old state.
 */
export function createEventInvalidator(
  queryClient: InvalidatingClient,
  queryKey: QueryKey,
  delayMs = 150,
): EventInvalidator {
  let timer: ReturnType<typeof setTimeout> | null = null
  let disposed = false
  /** Queries whose read in flight is owed one more read once it lands. */
  const owed = new Set<string>()

  const round = () => {
    for (const query of queryClient
      .getQueryCache()
      .findAll({ queryKey, fetchStatus: 'fetching' })) {
      const hash = query.queryHash
      if (owed.has(hash)) continue
      owed.add(hash)
      const reread = () => {
        // A macrotask later: by then the query has settled, so this starts a
        // read instead of joining the one that just ended.
        setTimeout(() => {
          owed.delete(hash)
          if (disposed) return
          void queryClient.invalidateQueries(
            { queryKey: query.queryKey, exact: true },
            { cancelRefetch: false },
          )
        }, 0)
      }
      void (query.promise ?? Promise.resolve()).then(reread, reread)
    }
    void queryClient.invalidateQueries({ queryKey }, { cancelRefetch: false })
  }

  return {
    notify: () => {
      if (disposed || timer) return
      timer = setTimeout(() => {
        timer = null
        round()
      }, delayMs)
    },
    dispose: () => {
      disposed = true
      if (timer) clearTimeout(timer)
      timer = null
    },
  }
}
