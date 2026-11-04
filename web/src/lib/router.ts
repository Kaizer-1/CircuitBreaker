// Four pages, so a few lines instead of a router library. Navigation keeps the query string, so a
// ?theme= override survives moving between pages.
import { useEffect, useState } from 'react'

const EVENT = 'circuitbreaker:navigate'

export function navigate(path: string): void {
  window.history.pushState({}, '', path + window.location.search)
  window.dispatchEvent(new Event(EVENT))
}

export function usePath(): string {
  const [path, setPath] = useState(window.location.pathname)
  useEffect(() => {
    const update = () => setPath(window.location.pathname)
    window.addEventListener('popstate', update)
    window.addEventListener(EVENT, update)
    return () => {
      window.removeEventListener('popstate', update)
      window.removeEventListener(EVENT, update)
    }
  }, [])
  return path
}
