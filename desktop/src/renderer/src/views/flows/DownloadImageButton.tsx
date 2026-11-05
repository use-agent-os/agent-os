import { Download } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { t } from '~/i18n'
import { cn } from '~/lib/utils'
import { errorText, useFlowImageDownload } from '~/stores/flows'
import type { ImageRef } from './types'

/** Saves one image at full size; `fileName` is what Save called it, when it did. */
export function DownloadImageButton({
  image,
  fileName,
  className,
}: {
  image: ImageRef
  fileName?: string
  className?: string
}) {
  const download = useFlowImageDownload()
  const [busy, setBusy] = useState(false)
  const label = t('flows.viewer.download')

  const onClick = async () => {
    setBusy(true)
    try {
      await download(image, fileName)
    } catch (err) {
      toast.error(t('flows.viewer.downloadFailed'), { description: errorText(err) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <button
      type="button"
      className={cn('flw-icon-btn', className)}
      aria-label={label}
      title={label}
      aria-busy={busy || undefined}
      disabled={busy}
      onClick={() => void onClick()}
    >
      <Download className="size-4" strokeWidth={1.75} aria-hidden />
    </button>
  )
}
