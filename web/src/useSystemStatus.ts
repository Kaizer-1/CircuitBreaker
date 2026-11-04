import { useEffect, useState } from 'react'
import { fetchSystemStatus, NotAuthorizedError, type SystemStatus } from './api'

export type Snapshot =
  | { kind: 'loading' }
  | { kind: 'ok'; status: SystemStatus }
  | { kind: 'needs_login' }
  | { kind: 'gateway_unreachable'; error: string }

/** Polls the gateway every `intervalMs` with the admin token. If the gateway itself is down, that
 * is a state, not a crash; a missing or expired token asks for the passcode again. */
export function useSystemStatus(token: string | null, intervalMs = 3000): Snapshot {
  // Each result remembers the token it was fetched with, so a new sign-in starts as "loading".
  const [result, setResult] = useState<{ token: string; snapshot: Snapshot } | null>(null)

  useEffect(() => {
    if (token === null) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined

    async function poll(current: string) {
      try {
        const status = await fetchSystemStatus(current, controller.signal)
        setResult({ token: current, snapshot: { kind: 'ok', status } })
      } catch (error) {
        if (controller.signal.aborted) return
        if (error instanceof NotAuthorizedError) {
          setResult({ token: current, snapshot: { kind: 'needs_login' } })
          return
        }
        const message = error instanceof Error ? error.message : String(error)
        setResult({ token: current, snapshot: { kind: 'gateway_unreachable', error: message } })
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(current), intervalMs)
    }

    void poll(token)
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [token, intervalMs])

  if (token === null) return { kind: 'needs_login' }
  if (result === null || result.token !== token) return { kind: 'loading' }
  return result.snapshot
}
