import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { consumeUrlCredentials } from './lib/credentials'
import { createWebRouter, WebApp } from './web/app/WebApp'
import './web/styles/base.css'

// Before the router (or anything else) reads the URL: take a `token` /
// `gatewayUrl` link parameter out of the address bar and history.
consumeUrlCredentials()

const router = createWebRouter()

createRoot(document.getElementById('app')!).render(
  <StrictMode>
    <WebApp router={router} />
  </StrictMode>,
)
