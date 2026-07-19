import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/globals.css'

createRoot(document.getElementById('app')!).render(
  <StrictMode>
    <div>AgentOS Control (React rewrite scaffold)</div>
  </StrictMode>,
)
