// Which skin is showing: `paper` (light) or `terminal` (dark). The rules, in order:
//   1. ?theme=paper|terminal in the URL wins over everything (pick the projector theme on demo day);
//   2. else the choice made with the toggle, remembered per device in localStorage;
//   3. else the device's preference (prefers-color-scheme: light → paper, dark → terminal).
// index.html runs the same rules inline before the first paint, so the wrong theme never flashes;
// theme.test.ts checks the two agree.
import { useEffect, useState } from 'react'

export type Theme = 'paper' | 'terminal'
export const THEMES: readonly Theme[] = ['paper', 'terminal']
export const STORAGE_KEY = 'circuitbreaker.theme'
const EVENT = 'circuitbreaker:theme'

const isTheme = (value: unknown): value is Theme => value === 'paper' || value === 'terminal'

export function resolveTheme(search: string, stored: string | null, prefersDark: boolean): Theme {
  const asked = new URLSearchParams(search).get('theme')
  if (isTheme(asked)) return asked
  if (isTheme(stored)) return stored
  return prefersDark ? 'terminal' : 'paper'
}

export function readStoredTheme(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    return null // storage blocked (private window): fall back to the device preference
  }
}

function prefersDark(): boolean {
  return typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches
}

export function currentTheme(): Theme {
  const shown = document.documentElement.dataset.theme
  return isTheme(shown) ? shown : resolveTheme(location.search, readStoredTheme(), prefersDark())
}

/** The toggle: show `theme` now and remember it on this device. */
export function chooseTheme(theme: Theme): void {
  try {
    localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    // not remembered; it still applies to this page
  }
  showTheme(theme)
}

function showTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme
  window.dispatchEvent(new CustomEvent(EVENT, { detail: theme }))
}

/** The theme shown now; re-renders when the toggle (or the device setting) changes it. */
export function useTheme(): Theme {
  const [theme, setTheme] = useState<Theme>(currentTheme)
  useEffect(() => {
    const onChange = (event: Event) => setTheme((event as CustomEvent<Theme>).detail)
    window.addEventListener(EVENT, onChange)
    // With no URL override and no remembered choice, follow the device if it changes (dusk).
    const media =
      typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: dark)') : null
    const onMedia = () => showTheme(resolveTheme(location.search, readStoredTheme(), prefersDark()))
    media?.addEventListener?.('change', onMedia)
    return () => {
      window.removeEventListener(EVENT, onChange)
      media?.removeEventListener?.('change', onMedia)
    }
  }, [])
  return theme
}

/** A token's value now, e.g. cssToken('--candle-up') (the chart needs colours in JavaScript). */
export function cssToken(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}
