import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { useRpc } from '@/app/providers'
import { sessionPath } from '~/components/sidebar/SessionRow'
import type { RawProject } from '@/views/projects/logic'
import { projectId, projectName } from '@/views/projects/logic'
import {
  ensureTradingSessionKey,
  readTradingAgentVersion,
  readTradingSessionFiled,
  writeTradingAgentVersion,
  writeTradingSessionFiled,
  writeTradingSessionKey,
} from '~/stores/trading-ui'
import { syncTradingAgent, TRADING_AGENT_ID, TRADING_AGENT_VERSION } from './agent'
import { TRADING_PROJECT_NAME, tradingProjectKnowledge } from './desk-logic'
import { mintTradingSessionKey } from './mode-logic'
import type { Wallet } from '../types'

const FILE_ATTEMPTS = 6
const FILE_RETRY_MS = 1500
const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

/**
 * The desk's newest chat, remembered across launches; older ones stay in the
 * sidebar and still open at the desk. The session key is minted here under
 * the desk's own `trading` agent; the gateway creates the session
 * on the first send. The agent itself is the desktop's: it is created (or
 * brought up to this build's spec) the first time the desk is active. Once
 * the session exists, it is filed into the "Trading desk" project (created
 * on demand) whose knowledge tells the agent the current wallets and limits.
 */

export function useTradingSession(
  ctx: {
    wallets: readonly Wallet[]
    chains: readonly number[]
    limits: { thresholdUsd: number; dailyCapUsd: number } | null
  },
  active = true,
): {
  sessionKey: string
  /** Start over in a fresh chat; the old one stays in the sidebar. */
  startFresh: () => void
  /** Call after the first send: files the session into the desk project. */
  ensureFiled: () => void
} {
  const rpc = useRpc()
  const navigate = useNavigate()
  const [sessionKey, setSessionKey] = useState(() => ensureTradingSessionKey(mintTradingSessionKey))
  const filingRef = useRef(false)
  const ctxRef = useRef(ctx)
  useEffect(() => {
    ctxRef.current = ctx
  }, [ctx])

  // The agent is written once per spec version; a failure (gateway busy,
  // registry read-only) leaves the stamp alone so the next launch retries.
  // The chat still works meanwhile: the gateway runs an unregistered agent
  // id with default tools, and the entry takes effect on the next turn.
  const syncingRef = useRef(false)
  useEffect(() => {
    if (!active || syncingRef.current) return
    if (readTradingAgentVersion() >= TRADING_AGENT_VERSION) return
    syncingRef.current = true
    void (async () => {
      try {
        await rpc.waitForConnection()
        await syncTradingAgent(rpc)
        writeTradingAgentVersion(TRADING_AGENT_VERSION)
      } catch {
        // Retried on the next launch.
      } finally {
        syncingRef.current = false
      }
    })()
  }, [rpc, active])

  // The desk project: found or created on demand, its knowledge brought up
  // to the current wallets and limits. Returns the project id.
  const projectIdRef = useRef('')
  const syncProject = useCallback(async (): Promise<string> => {
    await rpc.waitForConnection()
    const knowledge = tradingProjectKnowledge(ctxRef.current)
    const list = await rpc.call<{ projects?: RawProject[] }>('projects.list', {})
    let project = (list?.projects ?? []).find((p) => projectName(p) === TRADING_PROJECT_NAME)
    if (!project) {
      const created = await rpc.call<{ project?: RawProject }>('projects.create', {
        name: TRADING_PROJECT_NAME,
        agentId: TRADING_AGENT_ID,
        knowledge,
      })
      project = created?.project
    } else if (knowledge && (project.knowledge ?? '') !== knowledge) {
      await rpc.call('projects.update', { projectId: projectId(project), knowledge })
    }
    const id = project ? projectId(project) : ''
    projectIdRef.current = id
    return id
  }, [rpc])

  // The gateway creates the session on the first turn, which can land after
  // the first send returns: `sessions.patch` then says "not found". So filing
  // retries for a while instead of waiting for the next launch, which is how
  // a chat once ran a whole order without the project's wallet rule.
  const fileSession = useCallback(
    (attempts: number) => {
      if (filingRef.current || readTradingSessionFiled()) return
      filingRef.current = true
      void (async () => {
        try {
          for (let attempt = 0; attempt < attempts; attempt++) {
            if (attempt) await sleep(FILE_RETRY_MS)
            try {
              const id = await syncProject()
              if (!id) return
              await rpc.call('sessions.patch', {
                key: sessionKey,
                projectId: id,
                displayName: TRADING_PROJECT_NAME,
              })
              writeTradingSessionFiled(true)
              return
            } catch {
              // Not there yet, or the gateway is busy: try again.
            }
          }
        } finally {
          filingRef.current = false
        }
      })()
    },
    [rpc, sessionKey, syncProject],
  )
  const ensureFiled = useCallback(() => fileSession(FILE_ATTEMPTS), [fileSession])

  // A session that already exists (previous launch) may still be unfiled.
  // One attempt only: a brand-new key has no session yet, and the retries
  // belong to the first send, not to every launch.
  useEffect(() => {
    if (active && !readTradingSessionFiled()) fileSession(1)
  }, [fileSession, active])

  // Wallets and limits change while the chat lives (a new wallet, a new
  // primary): the knowledge follows, or the agent trades from the wrong one.
  const knowledge = tradingProjectKnowledge(ctx)
  const hasWallets = ctx.wallets.length > 0
  useEffect(() => {
    if (!active || !hasWallets || !readTradingSessionFiled()) return
    void syncProject().catch(() => {})
  }, [knowledge, hasWallets, active, syncProject])

  const startFresh = useCallback(() => {
    const fresh = mintTradingSessionKey()
    writeTradingSessionKey(fresh)
    setSessionKey(fresh)
    // The desk's session is a route: the fresh chat is where we go next.
    void navigate(sessionPath(fresh), { replace: true })
  }, [navigate])

  return { sessionKey, startFresh, ensureFiled }
}
