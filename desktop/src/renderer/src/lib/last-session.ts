const KEY = 'agentos-desktop.lastSession'

/** The session the chat view showed most recently, for "Open at launch: Last session". */
export function rememberLastSession(sessionKey: string | null): void {
  try {
    if (sessionKey) localStorage.setItem(KEY, sessionKey)
    else localStorage.removeItem(KEY)
  } catch {
    /* storage unavailable */
  }
}

export function readLastSession(): string | null {
  try {
    return localStorage.getItem(KEY) || null
  } catch {
    return null
  }
}
