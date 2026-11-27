// The presenter's screens and how to move between them, in both skins: the terminal skin's F-key
// bar (on-screen keys you click or tap; the real F5 and F12 stay the browser's) and the paper skin's
// section line under the masthead. Moving keeps the query string (a ?theme= override survives).
import { type MouseEvent, type ReactNode, useCallback, useState } from 'react'
import { AdminLogin } from './AdminLogin'
import { navigate } from '../lib/router'
import { loadAdminToken, saveAdminToken } from '../lib/session'

export type Screen = 'market' | 'time-travel' | 'chaos'

function go(path: string) {
  return (e: MouseEvent) => {
    e.preventDefault()
    navigate(path)
  }
}

export function FKeys({ active }: { active: Screen }) {
  return (
    <nav className="fkeys terminal-only" aria-label="Function keys">
      <a href="/dashboard" className={active === 'market' ? 'on' : ''} onClick={go('/dashboard')}>
        <b>F1</b>MARKET
      </a>
      {active === 'market' && (
        <>
          <span>
            <b>F2</b>DEPTH
          </span>
          <span>
            <b>F3</b>TRADES
          </span>
        </>
      )}
      <a
        href="/time-travel"
        className={active === 'time-travel' ? 'on' : ''}
        onClick={go('/time-travel')}
      >
        <b>F5</b>TIME TRAVEL
      </a>
      <a href="/chaos" className={active === 'chaos' ? 'on' : ''} onClick={go('/chaos')}>
        <b>F6</b>CONTROL
      </a>
      {active === 'market' && (
        <span>
          <b>F8</b>JOIN
        </span>
      )}
      <a href="/status">
        <b>F12</b>SYSTEM
      </a>
    </nav>
  )
}

export function Sections({ active }: { active: Screen }) {
  return (
    <nav className="sections paper-only" aria-label="Sections">
      <a
        href="/dashboard"
        aria-current={active === 'market' ? 'page' : undefined}
        onClick={go('/dashboard')}
      >
        Markets
      </a>
      <a
        href="/time-travel"
        aria-current={active === 'time-travel' ? 'page' : undefined}
        onClick={go('/time-travel')}
      >
        Time travel
      </a>
      <a
        href="/chaos"
        aria-current={active === 'chaos' ? 'page' : undefined}
        onClick={go('/chaos')}
      >
        Control room
      </a>
      <a href="/status">System</a>
    </nav>
  )
}

/** The presenter screens are admin-only: the passcode form until there is a token. */
export function PresenterGate({
  purpose,
  children,
}: {
  purpose: string
  children: (token: string, onExpired: () => void) => ReactNode
}) {
  const [token, setToken] = useState<string | null>(loadAdminToken)
  const expired = useCallback(() => {
    saveAdminToken(null)
    setToken(null)
  }, [])
  if (!token)
    return (
      <main className="gate">
        <h1 className="masthead">
          CircuitBreaker <em>Markets</em>
        </h1>
        <AdminLogin
          purpose={purpose}
          onToken={(t) => {
            saveAdminToken(t)
            setToken(t)
          }}
        />
      </main>
    )
  return <>{children(token, expired)}</>
}
