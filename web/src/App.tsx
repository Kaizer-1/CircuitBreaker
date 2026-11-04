import { Dashboard } from './pages/Dashboard'
import { Join } from './pages/Join'
import { Trade } from './pages/Trade'
import { StatusPage } from './StatusPage'
import { usePath } from './lib/router'

// /join (the QR code's target) → /trade on the phone; /dashboard on the projector; / and /status:
// System Status (admin).
export function App() {
  const path = usePath()
  if (path === '/dashboard') return <Dashboard />
  if (path === '/join') return <Join />
  if (path === '/trade') return <Trade />
  return <StatusPage />
}
