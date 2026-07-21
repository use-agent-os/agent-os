import { effectiveElevatedMode } from './logic'

// The elevated-mode pill titles, ported verbatim from _updateElevatedPill
// (chat.js:2314-2343). Kept as constants so the presentational component and any
// future readout share one copy of the operator-facing wording.
const TITLE_UNAVAILABLE =
  'Bypass requires a local owner session. The gateway is bound to a non-loopback address, so this client cannot toggle elevated mode.'
const TITLE_SESSION =
  'Session permission override is active. Approval prompts are bypassed for this browser chat session. Click to clear the override.'
const TITLE_GLOBAL =
  'Global permission default controls execution mode and is configured by agentos sandbox on|bypass|full|reset.'
const TITLE_NEUTRAL =
  'Approval prompts are active. Click to enable approval bypass for this browser session.'

export interface ElevatedPillProps {
  // The browser SESSION override (from the shared elevated-mode store).
  sessionMode: string
  // The GLOBAL permissions.default_mode (from config.get).
  globalMode: string
  // Latched true after a 403 from POST /api/elevated-mode (non-owner session).
  unavailable: boolean
  onToggle: () => void
}

/**
 * The execution-mode pill in the composer toolbar (chat.js:2314-2343
 * `_updateElevatedPill` + the pill markup at chat.js:1259-1260).
 *
 * The SESSION override wins over the GLOBAL default for the label; the pill is
 * `is-active` whenever an effective elevated mode is in force. Status color
 * flows through the design-system `--tone` gutter (`tone-danger`) — lime stays
 * signal-only — matching the legacy `chat-pill--danger` accent. Radius stays 0
 * (the terminal look inherits `border-radius: 0` from the shared pill styles).
 */
export function ElevatedPill({
  sessionMode,
  globalMode,
  unavailable,
  onToggle,
}: ElevatedPillProps) {
  if (unavailable) {
    // chat.js:2316-2322 — the latched non-owner state: disabled, distinct label.
    return (
      <button
        type="button"
        className="chat-pill chat-pill--disabled"
        aria-disabled="true"
        title={TITLE_UNAVAILABLE}
        onClick={onToggle}
      >
        Bypass N/A
      </button>
    )
  }

  const effective = effectiveElevatedMode(sessionMode, globalMode)
  const active = !!effective

  let text: string
  let title: string
  if (sessionMode) {
    // chat.js:2330-2333
    text = `Session ${sessionMode.toUpperCase()}`
    title = TITLE_SESSION
  } else if (globalMode) {
    // chat.js:2334-2337
    text = `Global ${globalMode.toUpperCase()}`
    title = TITLE_GLOBAL
  } else {
    // chat.js:2338-2341
    text = 'Approval prompts'
    title = TITLE_NEUTRAL
  }

  return (
    <button
      type="button"
      className={`chat-pill tone-danger${active ? ' is-active' : ''}`}
      title={title}
      onClick={onToggle}
    >
      {text}
    </button>
  )
}
