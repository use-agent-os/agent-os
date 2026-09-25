import { useCallback, useState } from 'react'
import { useLocation, useParams } from 'react-router'
import { canonicalSessionKey } from '@/views/chat/logic'
import { readTradingSessionKey } from '~/stores/trading-ui'
import { useGateway } from '~/stores/gateway'
import { ChatView } from '~/views/chat/ChatView'
import { t } from '~/i18n'
import { TradingView } from '../TradingView'
import { deriveMode, type DeskMode } from './mode-logic'
import { useDeskFrame } from './TradingDesk'
import { useDeskToggle } from './useDeskToggle'
import { useEntrance } from './useEntrance'

/**
 * `/sessions/:key?` in both of its lives. The mode is not a flag: it is
 * derived from whether the open session is the desk's own. Chat mode is the
 * ordinary conversation with the pill in its strip; Trading mode is the same
 * ChatView — same tree position, same composer node — handed the desk's
 * instruments, with the BOOK beside it. Switching is a navigation, and
 * entering Trading plays the one choreographed entrance, then stillness.
 */
export function SessionRoute() {
  const { key: rawParam } = useParams()
  const location = useLocation()
  const paramKey = rawParam ? canonicalSessionKey(decodeURIComponent(rawParam)) : ''
  // Read on every render: a key minted elsewhere (⌘⇧T from another page,
  // the redirect, a fresh desk chat) must be seen by the very next route.
  const tradingKey = readTradingSessionKey()
  const mode: DeskMode = deriveMode(paramKey, tradingKey)
  const requested = Boolean((location.state as { enterDesk?: boolean } | null)?.enterDesk)
  const { toggle } = useDeskToggle()
  const onSwitchMode = useCallback(
    (next: DeskMode) => {
      if (next !== mode) toggle()
    },
    [mode, toggle],
  )

  const [sessionPending, setSessionPending] = useState(0)
  const { enter } = useEntrance({ mode, still: sessionPending > 0, requested })
  const gatewayRunning = useGateway((s) => s.status.state === 'running')
  const active = mode === 'trading' && gatewayRunning
  // In Chat mode the chat's session actions sit on the strip, right of the
  // pill, instead of on a row of their own under it. At the desk the strip
  // is the desk's, and they stay above the chat column.
  const [sessionSlot, setSessionSlot] = useState<HTMLDivElement | null>(null)
  const { frameRef, ...frame } = useDeskFrame({
    sessionKey: paramKey,
    mode,
    active,
    entering: enter === 'trading',
    onSwitchMode,
    onSessionPending: setSessionPending,
    sessionSlot: setSessionSlot,
  })

  return (
    <div
      className="mode-shell"
      data-mode={mode}
      data-enter={enter ?? undefined}
      data-testid="mode-shell"
    >
      {frame.strip}
      {/* The full desk carries its own gate states; the frame's banner would say it twice. */}
      {active && frame.fullDesk ? null : frame.banner}
      {active && frame.fullDesk ? (
        <div className="trd-page__desk">
          <TradingView entering={enter === 'trading'} />
        </div>
      ) : (
        <div
          className={active ? 'trd-frame' : 'mode-body'}
          ref={frameRef}
          data-collapsed={frame.collapsed || undefined}
        >
          <ChatView desk={frame.desk} actionsSlot={mode === 'chat' ? sessionSlot : undefined} />
          {frame.book}
        </div>
      )}
      {frame.sheet}
      {enter === 'trading' ? (
        <div className="trd-poweron" aria-hidden>
          {t('trading.mode.stamp')}
        </div>
      ) : null}
    </div>
  )
}
