import { useState, type ComponentProps } from 'react'
import { BridgePanel } from './BridgePanel'
import { SwapPanel } from './SwapPanel'
import type { TicketMode } from './TicketModes'

/**
 * The full desk's ticket: the swap form, or the bridge form, behind the one
 * head. A swap asked for from elsewhere (a Holdings row, a Markets row)
 * brings the swap form back, since that is the form it fills.
 */
export function Ticket(props: Omit<ComponentProps<typeof SwapPanel>, 'onMode'>) {
  const [mode, setMode] = useState<TicketMode>('swap')
  const seq = props.prefill?.seq ?? null
  const [seenSeq, setSeenSeq] = useState(seq)
  if (seq !== seenSeq) {
    setSeenSeq(seq)
    setMode('swap')
  }
  return mode === 'bridge' ? (
    <BridgePanel
      wallets={props.wallets}
      primary={props.primary}
      selectedWallet={props.selectedWallet}
      unlocked={props.unlocked}
      onSent={props.onSent}
      onMode={setMode}
    />
  ) : (
    <SwapPanel {...props} onMode={setMode} />
  )
}
