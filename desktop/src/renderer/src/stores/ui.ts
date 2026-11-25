import { create } from 'zustand'
import { DEFAULT_SECTION, type SettingsSection } from '~/views/settings/sections'

export const SIDEBAR_MIN = 180
export const SIDEBAR_MAX = 420
export const SIDEBAR_DEFAULT = 220
/** Dragging narrower than this snaps the sidebar closed, like Finder. */
export const SIDEBAR_COLLAPSE_AT = 120

const WIDTH_KEY = 'agentos-desktop.sidebarWidth'

function loadWidth(): number {
  try {
    const raw = Number(localStorage.getItem(WIDTH_KEY))
    if (Number.isFinite(raw) && raw >= SIDEBAR_MIN && raw <= SIDEBAR_MAX) return raw
  } catch {
    /* storage unavailable */
  }
  return SIDEBAR_DEFAULT
}

function saveWidth(width: number): void {
  try {
    localStorage.setItem(WIDTH_KEY, String(width))
  } catch {
    /* storage unavailable */
  }
}

const FOLDERS_KEY = 'agentos-desktop.openFolders'

function loadOpenFolders(): ReadonlySet<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(FOLDERS_KEY) || '[]')
    if (Array.isArray(raw)) return new Set(raw.filter((x) => typeof x === 'string'))
  } catch {
    /* storage unavailable or corrupt */
  }
  return new Set()
}

function saveOpenFolders(ids: ReadonlySet<string>): void {
  try {
    localStorage.setItem(FOLDERS_KEY, JSON.stringify([...ids]))
  } catch {
    /* storage unavailable */
  }
}

interface UiStore {
  sidebarOpen: boolean
  sidebarWidth: number
  sessionQuery: string
  /** The Scheduled jobs panel is a layer over the window, not a route. */
  jobsOpen: boolean
  /** Skills is the same kind of layer: browse, install, then back to the chat. */
  skillsOpen: boolean
  /** A status filter the Skills sheet should open on; read once on open. */
  skillsStatus: 'needs-setup' | null
  /** Files is one too: the agent workspace, read-only. */
  filesOpen: boolean
  /** The file being previewed and the folders disclosed; kept while the app runs. */
  filesSelected: string | null
  filesExpanded: ReadonlySet<string>
  /** Settings is a sheet over the window too; remembers the section between opens. */
  settingsOpen: boolean
  settingsSection: SettingsSection
  /**
   * Text waiting to be dropped into the composer, written by Skills → "Use in
   * chat" and drained by the chat view once its composer is mounted. One-shot:
   * nothing is sent, the user still presses Return.
   */
  pendingPrompt: string | null
  /**
   * Bumped by a new-session action (⌘N, the chat's new-chat paths, the
   * sidebar's New session, the desk's "Start fresh"). The chat view focuses
   * its composer on every bump: it usually stays mounted across a new
   * session, so the composer's own focus-on-mount never runs again.
   */
  composerFocusRequest: number
  /** Project folders currently disclosed in the sidebar. */
  openFolders: ReadonlySet<string>
  /**
   * Folders whose "N more" was pressed: they list every chat, not just the
   * preview. For this run of the app only; a relaunch starts folded.
   */
  expandedFolders: ReadonlySet<string>
  /** The inline "new project" row is showing in the sidebar. */
  creatingProject: boolean
  toggleSidebar(): void
  setSidebarWidth(width: number): void
  resetSidebarWidth(): void
  setSessionQuery(q: string): void
  openJobs(): void
  closeJobs(): void
  toggleJobs(): void
  openSkills(): void
  /** Open Skills already filtered to the ones that need setup. */
  openSkillsNeedingSetup(): void
  closeSkills(): void
  toggleSkills(): void
  openFiles(): void
  closeFiles(): void
  toggleFiles(): void
  selectFile(path: string | null): void
  setFilesExpanded(paths: ReadonlySet<string>): void
  openSettings(section?: SettingsSection): void
  closeSettings(): void
  toggleSettings(): void
  setSettingsSection(section: SettingsSection): void
  setPendingPrompt(text: string | null): void
  requestComposerFocus(): void
  toggleFolder(id: string): void
  setFolderOpen(id: string, open: boolean): void
  /** Replace the open set wholesale: the Projects header's collapse / expand all. */
  setOpenFolders(ids: Iterable<string>): void
  setFolderExpanded(id: string, expanded: boolean): void
  startCreatingProject(): void
  stopCreatingProject(): void
}

/** Chrome state. The sidebar width and open folders survive a relaunch. */
export const useUi = create<UiStore>((set) => ({
  sidebarOpen: true,
  sidebarWidth: loadWidth(),
  sessionQuery: '',
  jobsOpen: false,
  skillsOpen: false,
  skillsStatus: null,
  filesOpen: false,
  filesSelected: null,
  filesExpanded: new Set(),
  settingsOpen: false,
  settingsSection: DEFAULT_SECTION,
  pendingPrompt: null,
  composerFocusRequest: 0,
  openFolders: loadOpenFolders(),
  expandedFolders: new Set(),
  creatingProject: false,
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  setSidebarWidth: (width) => {
    const clamped = Math.round(Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, width)))
    saveWidth(clamped)
    set({ sidebarWidth: clamped, sidebarOpen: true })
  },
  resetSidebarWidth: () => {
    saveWidth(SIDEBAR_DEFAULT)
    set({ sidebarWidth: SIDEBAR_DEFAULT, sidebarOpen: true })
  },
  setSessionQuery: (sessionQuery) => set({ sessionQuery }),
  // One sheet at a time: opening any of Jobs, Skills, Files or Settings closes the others.
  openJobs: () => set({ jobsOpen: true, skillsOpen: false, filesOpen: false, settingsOpen: false }),
  closeJobs: () => set({ jobsOpen: false }),
  toggleJobs: () =>
    set((s) => ({
      jobsOpen: !s.jobsOpen,
      skillsOpen: false,
      filesOpen: false,
      settingsOpen: false,
    })),
  openSkills: () =>
    set({
      skillsOpen: true,
      skillsStatus: null,
      jobsOpen: false,
      filesOpen: false,
      settingsOpen: false,
    }),
  openSkillsNeedingSetup: () =>
    set({
      skillsOpen: true,
      skillsStatus: 'needs-setup',
      jobsOpen: false,
      filesOpen: false,
      settingsOpen: false,
    }),
  closeSkills: () => set({ skillsOpen: false, skillsStatus: null }),
  toggleSkills: () =>
    set((s) => ({
      skillsOpen: !s.skillsOpen,
      jobsOpen: false,
      filesOpen: false,
      settingsOpen: false,
    })),
  openFiles: () =>
    set({ filesOpen: true, jobsOpen: false, skillsOpen: false, settingsOpen: false }),
  closeFiles: () => set({ filesOpen: false }),
  toggleFiles: () =>
    set((s) => ({
      filesOpen: !s.filesOpen,
      jobsOpen: false,
      skillsOpen: false,
      settingsOpen: false,
    })),
  selectFile: (filesSelected) => set({ filesSelected }),
  setFilesExpanded: (paths) => set({ filesExpanded: new Set(paths) }),
  openSettings: (section) =>
    set((s) => ({
      settingsOpen: true,
      jobsOpen: false,
      skillsOpen: false,
      filesOpen: false,
      settingsSection: section ?? s.settingsSection,
    })),
  closeSettings: () => set({ settingsOpen: false }),
  toggleSettings: () =>
    set((s) => ({
      settingsOpen: !s.settingsOpen,
      jobsOpen: false,
      skillsOpen: false,
      filesOpen: false,
    })),
  setSettingsSection: (settingsSection) => set({ settingsSection }),
  setPendingPrompt: (pendingPrompt) => set({ pendingPrompt }),
  requestComposerFocus: () => set((s) => ({ composerFocusRequest: s.composerFocusRequest + 1 })),
  toggleFolder: (id) =>
    set((s) => {
      const next = new Set(s.openFolders)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      saveOpenFolders(next)
      return { openFolders: next }
    }),
  setFolderOpen: (id, open) =>
    set((s) => {
      if (s.openFolders.has(id) === open) return s
      const next = new Set(s.openFolders)
      if (open) next.add(id)
      else next.delete(id)
      saveOpenFolders(next)
      return { openFolders: next }
    }),
  setOpenFolders: (ids) => {
    const next = new Set(ids)
    saveOpenFolders(next)
    set({ openFolders: next })
  },
  setFolderExpanded: (id, expanded) =>
    set((s) => {
      if (s.expandedFolders.has(id) === expanded) return s
      const next = new Set(s.expandedFolders)
      if (expanded) next.add(id)
      else next.delete(id)
      return { expandedFolders: next }
    }),
  startCreatingProject: () => set({ creatingProject: true, sidebarOpen: true }),
  stopCreatingProject: () => set({ creatingProject: false }),
}))
