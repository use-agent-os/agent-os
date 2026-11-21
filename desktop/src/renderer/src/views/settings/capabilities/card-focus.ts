/**
 * Where the keyboard goes inside a capability card, so focus never drops to
 * <body> when the control under it goes away (Save greys out, Revert's foot
 * disappears) or when a jump lands on the card.
 */

const ENABLED = ':not([disabled]):not([aria-disabled="true"])'

/** The card's resting control: the chosen service, else its on/off switch, else its first field. */
export function cardHome(card: Element | null | undefined): HTMLElement | null {
  if (!card) return null
  return (
    card.querySelector<HTMLElement>(`[role="radio"][aria-checked="true"]${ENABLED}`) ??
    card.querySelector<HTMLElement>(`[role="switch"]${ENABLED}`) ??
    card.querySelector<HTMLElement>(`input${ENABLED}, select${ENABLED}, button${ENABLED}`)
  )
}

/**
 * Where a jump onto the card puts the keyboard: the key field when the key
 * is what is missing (the reason to come here), else the card's home.
 */
export function cardEntry(card: Element | null | undefined): HTMLElement | null {
  if (!card) return null
  const keyRow = card.querySelector('.cap-missing')?.closest('.stg-row')
  return keyRow?.querySelector<HTMLElement>(`input${ENABLED}`) ?? cardHome(card)
}
