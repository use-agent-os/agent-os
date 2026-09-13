import { Monitor, Moon, Sun } from 'lucide-react'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useTheme } from './theme-store'

/** Toolbar cycler: system -> light -> dark. */
export function ThemeToggle() {
  const preference = useTheme((s) => s.preference)
  const cycle = useTheme((s) => s.cycle)
  const Icon = preference === 'system' ? Monitor : preference === 'dark' ? Moon : Sun
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label={t('theme.toggle')}
      title={t(`theme.mode.${preference}`)}
      onClick={() => void cycle()}
    >
      <Icon className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
    </Button>
  )
}
