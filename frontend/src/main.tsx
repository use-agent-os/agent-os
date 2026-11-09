import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createWebRouter, WebApp } from './web/app/WebApp'
import './web/styles/base.css'

const router = createWebRouter()

createRoot(document.getElementById('app')!).render(
  <StrictMode>
    <WebApp router={router} />
  </StrictMode>,
)
