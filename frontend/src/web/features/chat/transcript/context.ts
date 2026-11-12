import { createContext, useContext } from 'react'
import type { PanelTab } from '../panels/panel-store'
import type { Turn } from '../model/types'

/** What transcript pieces can ask the chat pane to do. */
export interface ChatActions {
  sessionKey: string
  openPanel(tab: PanelTab, opener?: HTMLElement | null): void
  retry(turn: Turn): void
  edit(turn: Turn): void
  /** Answer an ask_user card by sending the composed reply. */
  answer(text: string): Promise<boolean>
  approvePlan(): Promise<void>
}

export const ChatActionsContext = createContext<ChatActions | null>(null)

/** Who answers in this transcript: the name on each reply's identity line. */
export const AgentNameContext = createContext('')

export function useChatActions(): ChatActions {
  const actions = useContext(ChatActionsContext)
  if (!actions) throw new Error('useChatActions outside a chat pane')
  return actions
}
