import { createHashRouter, Navigate } from 'react-router'
import { FlowEditorRoute } from '~/views/flows/FlowEditor'
import { FlowsHome } from '~/views/flows/FlowsHome'
import { ProjectView } from '~/views/projects/ProjectView'
import { SessionRoute } from '~/views/trading/desk/SessionRoute'
import { TradingRedirect } from '~/views/trading/desk/TradingRedirect'
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
//
// Image Flows are pages: `flows` lists them with the templates, `flows/:id`
// is the node editor for one.
//
// `#/quick-ask` is not here: it is the Quick Ask panel's own window, which
// App renders without the router, the shell or a gateway connection.
export const router = createHashRouter([
  {
    path: '/',
    Component: AppShell,
    children: [
      { index: true, element: <Navigate to="/sessions" replace /> },
      // A session is a chat — or, when it is the desk's own session, the
      // trading desk around that same chat (views/trading/desk/SessionRoute).
      { path: 'sessions/:key?', Component: SessionRoute },
      { path: 'projects/:id', Component: ProjectView },
      { path: 'flows', Component: FlowsHome },
      { path: 'flows/:id', Component: FlowEditorRoute },
      // Trading is not a page: /trading forwards into the desk session.
      { path: 'trading', Component: TradingRedirect },
    ],
  },
])
