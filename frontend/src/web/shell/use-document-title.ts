import { useEffect } from 'react'
import { useLocation } from 'react-router'
import { t, useLocale } from '@/i18n'
import '@/i18n/en/webShell'
import { useSessionsList } from './session-data'
import { sessionTitle } from './session-list'
import { pathToSessionKey } from './session-url'
import { useRouteHandle } from './use-destination'

/** "<destination> · AgentOS"; on chat routes "<session title> · AgentOS". */
export function useDocumentTitle(): void {
  const handle = useRouteHandle()
  const { pathname } = useLocation()
  const locale = useLocale()
  const { rows } = useSessionsList()
  const sessionKey = pathname.startsWith('/chat/') ? pathToSessionKey(pathname) : null
  const row = sessionKey ? rows.find((r) => r.key === sessionKey) : undefined
  const title = row ? sessionTitle(row) : handle ? t(handle.titleKey) : t('webShell.appName')

  useEffect(() => {
    document.title =
      title === t('webShell.appName') ? title : t('webShell.documentTitle', { title })
  }, [title, locale])
}
