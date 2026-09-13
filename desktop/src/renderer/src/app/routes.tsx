import { createHashRouter, Navigate } from 'react-router'
import { ChatView } from '~/views/chat/ChatView'
import { ProjectView } from '~/views/projects/ProjectView'
import { AppShell } from './AppShell'

// Hash routing: the packaged app loads index.html from disk (file://), where
// history-based routing has no server to fall back to.
//
// Home and a session share ONE route (`sessions/:key?`) on purpose: the first
// send navigates from the keyless home to `/sessions/<key>` and React Router
// keeps the same element mounted, so the composer docks with an animation
// instead of remounting.
//
// Scheduled jobs and Settings are not routes: they are sheets over the
// window (AppShell), so opening one never leaves the conversation underneath.
//
// A project is a page (`projects/:id`), reached from its folder in the
// sidebar: its brief and the chats filed in it.
export const router = createHashRouter([
  {
    path: '/',
    Component: AppShell,
    children: [
      { index: true, element: <Navigate to="/sessions" replace /> },
      { path: 'sessions/:key?', Component: ChatView },
      { path: 'projects/:id', Component: ProjectView },
    ],
  },
])
