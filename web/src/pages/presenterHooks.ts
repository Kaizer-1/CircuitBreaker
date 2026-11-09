// Hooks the presenter screens share (the dashboard and time travel).
import { type RefObject, useEffect, useState } from 'react'
import { fetchSystemStatus, NotAuthorizedError } from '../api'

export function useNow(everyMs = 1000): Date {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), everyMs)
    return () => window.clearInterval(id)
  }, [everyMs])
  return now
}

/** How many whole table rows fit in a panel (the tape takes the height the depth leaves, and the
 * skins' rows differ in height): a half-cut row at the bottom would look like a bug. */
export function useRowsThatFit(
  panel: RefObject<HTMLElement | null>,
  max: number,
  hasRows: boolean,
) {
  const [rows, setRows] = useState(max)
  useEffect(() => {
    const box = panel.current
    if (!box || !hasRows) return
    const measure = () => {
      const body = box.querySelector('tbody')
      const row = body?.querySelector('tr')?.getBoundingClientRect().height
      if (!body || !row) return
      const room = box.getBoundingClientRect().bottom - body.getBoundingClientRect().top - 2
      setRows(Math.max(1, Math.min(max, Math.floor(room / row))))
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(box)
    return () => observer.disconnect()
  }, [panel, max, hasRows])
  return rows
}

export function useHealth(token: string, onExpired: () => void): string {
  const [health, setHealth] = useState('checking')
  useEffect(() => {
    let stop = false
    const check = () =>
      fetchSystemStatus(token)
        .then((s) => {
          const bad = s.services.filter((x) => x.status !== 'ready').length
          if (!stop) setHealth(bad === 0 ? 'all systems healthy' : `${bad} services need attention`)
        })
        .catch((e) => {
          if (e instanceof NotAuthorizedError) onExpired()
          else if (!stop) setHealth('gateway unreachable')
        })
    void check()
    const id = window.setInterval(check, 10_000)
    return () => {
      stop = true
      window.clearInterval(id)
    }
  }, [token, onExpired])
  return health
}
