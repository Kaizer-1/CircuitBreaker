import { Dashboard } from './pages/Dashboard'
import { Join } from './pages/Join'
import { TimeTravel } from './pages/TimeTravel'
import { Trade } from './pages/Trade'
import { StatusPage } from './StatusPage'
import { usePath } from './lib/router'

// /join (the QR code's target) → /trade on the phone; /dashboard and /time-travel on the
// projector (admin); / and /status: System Status (admin).
export function App() {
  const path = usePath()
  if (path === '/dashboard') return <Dashboard />
  if (path === '/time-travel') return <TimeTravel />
  if (path === '/join') return <Join />
  if (path === '/trade') return <Trade />
  return <StatusPage />
}
