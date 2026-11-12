import { clsx, type ClassValue } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

// The console's named type sizes (styles/tokens.css). Without this,
// tailwind-merge reads `text-stat` or `text-ui` as a text colour and drops it
// when a real colour class follows.
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      'font-size': [{ text: ['ui', 'small', 'label', 'section', 'title', 'stat', 'prose'] }],
    },
  },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
