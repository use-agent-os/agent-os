import { useEffect, useRef } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router'
import { useKeyboardShortcut } from '@/components/KeyboardShortcuts'
import { PetOverlay } from '~/components/pet/PetOverlay'
import { Sidebar } from '~/components/Sidebar'
import { Toolbar } from '~/components/Toolbar'
import { sessionPath } from '~/components/sidebar/SessionRow'
import { t } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'
import { readLastSession } from '~/lib/last-session'
import { useNotificationSignals } from '~/lib/use-notifications'
import { bindGatewayEvents } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import { useUi } from '~/stores/ui'
import { bindUpdateEvents } from '~/stores/updates'
import { bindBootstrapEvents } from '~/stores/bootstrap'
import { SetupOverlay } from '~/views/setup/SetupOverlay'
import { JobsPanel } from '~/views/jobs/JobsPanel'
import { SettingsPanel } from '~/views/settings/SettingsPanel'
import { SkillsPanel } from '~/views/skills/SkillsPanel'

/** Window chrome: translucent full-height sidebar, then toolbar + routed content. */
export function AppShell() {
  useEffect(() => bindGatewayEvents(), [])
  useEffect(() => bindUpdateEvents(), [])
  useEffect(() => bindBootstrapEvents(), [])
  useOpenSettingsFromMenu()
  useShellShortcuts()
  useLaunchView()
  useNotificationSignals()

  return (
    <div className="flex h-full">
      <Sidebar />
      <div className="mac-content flex min-w-0 flex-1 flex-col">
        <Toolbar />
        <main className="relative z-[2] min-h-0 flex-1 overflow-auto" data-selectable>
          <Outlet />
        </main>
      </div>
      {/* Layers over the whole window, whichever route is showing. */}
      <JobsPanel />
      <SkillsPanel />
      <SettingsPanel />
      <PetOverlay />
      {/* First run: installs the engine before anything else is usable. */}
      <SetupOverlay />
    </div>
  )
}

/** The app menu's "Settings…" item (⌘, at the OS level) arrives over IPC. */
function useOpenSettingsFromMenu() {
  const openSettings = useUi((s) => s.openSettings)
  useEffect(() => desktopApi().settings.onOpenRequested(() => openSettings()), [openSettings])
}

/** ⌘, settings · ⌘N new session · ⌘⇧S sidebar · ⌘⇧K skills · ⌘⇧J jobs.
 *  Registered with the console's registry so they show in its cheat sheet
 *  and respect open overlays. */
function useShellShortcuts() {
  const navigate = useNavigate()
  const toggleSettings = useUi((s) => s.toggleSettings)
  const toggleSidebar = useUi((s) => s.toggleSidebar)
  const toggleSkills = useUi((s) => s.toggleSkills)
  const toggleJobs = useUi((s) => s.toggleJobs)
  const category = t('settings.shortcuts.app')

  // Sheets toggle even while one is up: ⌘⇧K over Settings swaps to Skills,
  // and pressed again it closes, the way ⌘, already behaves.
  useKeyboardShortcut(
    {
      combo: 'mod+shift+k',
      description: t('settings.shortcuts.skills'),
      category,
      allowInInputs: true,
      allowWithOverlays: true,
    },
    (e) => {
      e.preventDefault()
      toggleSkills()
    },
  )
  useKeyboardShortcut(
    {
      combo: 'mod+shift+j',
      description: t('settings.shortcuts.jobs'),
      category,
      allowInInputs: true,
      allowWithOverlays: true,
    },
    (e) => {
      e.preventDefault()
      toggleJobs()
    },
  )

  useKeyboardShortcut(
    {
      combo: 'mod+,',
      description: t('settings.shortcuts.settings'),
      category,
      allowInInputs: true,
      allowWithOverlays: true,
    },
    (e) => {
      e.preventDefault()
      toggleSettings()
    },
  )
  useKeyboardShortcut(
    {
      combo: 'mod+n',
      description: t('settings.shortcuts.newSession'),
      category,
      allowInInputs: true,
    },
    (e) => {
      e.preventDefault()
      void navigate('/sessions')
    },
  )
  useKeyboardShortcut(
    {
      combo: 'mod+shift+s',
      description: t('settings.shortcuts.toggleSidebar'),
      category,
      allowInInputs: true,
    },
    (e) => {
      e.preventDefault()
      toggleSidebar()
    },
  )
}

/**
 * "Open at launch: Last session". Runs once, after settings have loaded and
 * only while still on the keyless home route, so a deep link or a click that
 * beat the settings read is never overridden.
 */
function useLaunchView() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const loaded = useSettings((s) => s.loaded)
  const launchView = useSettings((s) => s.settings.general.launchView)
  const done = useRef(false)
  useEffect(() => {
    if (done.current || !loaded) return
    done.current = true
    if (launchView !== 'last') return
    const last = readLastSession()
    if (last && (pathname === '/sessions' || pathname === '/')) {
      void navigate(sessionPath(last), { replace: true })
    }
  }, [loaded, launchView, pathname, navigate])
}
