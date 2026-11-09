// The trader's session (A-01) is kept on the phone across reloads (localStorage); the admin's
// (A-04) only for the browser tab (sessionStorage), as on the System Status page. Storage can be
// blocked (private windows): then the session lasts until the page is closed.

export interface TraderSession {
  token: string
  traderId: string
  nickname: string
  /** The suffix the join page added because the typed name was taken (lib/nickname.ts). */
  suffix?: string
  expiresAt: string
}

const TRADER_KEY = 'circuitbreaker.trader'
const ADMIN_KEY = 'circuitbreaker.admin_token'

export function loadTrader(): TraderSession | null {
  try {
    const raw = localStorage.getItem(TRADER_KEY)
    if (!raw) return null
    const session = JSON.parse(raw) as TraderSession
    return new Date(session.expiresAt).getTime() > Date.now() ? session : null
  } catch {
    return null
  }
}

export function saveTrader(session: TraderSession | null): void {
  try {
    if (session === null) localStorage.removeItem(TRADER_KEY)
    else localStorage.setItem(TRADER_KEY, JSON.stringify(session))
  } catch {
    // not remembered across reloads
  }
}

export function loadAdminToken(): string | null {
  try {
    return sessionStorage.getItem(ADMIN_KEY)
  } catch {
    return null
  }
}

export function saveAdminToken(token: string | null): void {
  try {
    if (token === null) sessionStorage.removeItem(ADMIN_KEY)
    else sessionStorage.setItem(ADMIN_KEY, token)
  } catch {
    // not remembered across reloads
  }
}

const LEADERBOARD_KEY = 'circuitbreaker.leaderboard'

/** The presenter's choice to show the leaderboard on the projector (remembered on this device). */
export function loadLeaderboardShown(): boolean {
  try {
    return localStorage.getItem(LEADERBOARD_KEY) !== 'hidden'
  } catch {
    return true
  }
}

export function saveLeaderboardShown(shown: boolean): void {
  try {
    localStorage.setItem(LEADERBOARD_KEY, shown ? 'shown' : 'hidden')
  } catch {
    // not remembered across reloads
  }
}
