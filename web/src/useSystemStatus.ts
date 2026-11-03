import { useEffect, useState } from 'react'
import { fetchSystemStatus, type SystemStatus } from './api'

export type Snapshot =
  | { kind: 'loading' }
  | { kind: 'ok'; status: SystemStatus }
  | { kind: 'gateway_unreachable'; error: string }

/** Polls the gateway every `intervalMs`. If the gateway itself is down, that is a state, not a crash. */
export function useSystemStatus(intervalMs = 3000): Snapshot {
  const [snapshot, setSnapshot] = useState<Snapshot>({ kind: 'loading' })

  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined

    async function poll() {
      try {
        const status = await fetchSystemStatus(controller.signal)
        setSnapshot({ kind: 'ok', status })
      } catch (error) {
        if (controller.signal.aborted) return
        const message = error instanceof Error ? error.message : String(error)
        setSnapshot({ kind: 'gateway_unreachable', error: message })
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, intervalMs)
    }

    void poll()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [intervalMs])

  return snapshot
}
