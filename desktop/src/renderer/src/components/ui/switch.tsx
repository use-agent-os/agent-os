import type { ButtonHTMLAttributes } from 'react'
import { cn } from '~/lib/utils'

export interface SwitchProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onChange'> {
  checked: boolean
  onCheckedChange: (checked: boolean) => void
}

/** NSSwitch. A button with role=switch so it is keyboard- and VoiceOver-native. */
export function Switch({ checked, onCheckedChange, className, ...props }: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      className={cn('mac-switch app-no-drag', className)}
      onClick={() => onCheckedChange(!checked)}
      {...props}
    />
  )
}
