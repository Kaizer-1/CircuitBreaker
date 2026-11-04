// Component tests run in both skins: the real tokens.css is loaded and data-theme is set, so
// getComputedStyle (and the chart's colours) see the same values as the browser.
import tokens from '../src/theme/tokens.css?raw'
import type { Theme } from '../src/theme/theme'

let loaded = false
export function showTheme(theme: Theme): void {
  if (!loaded) {
    const style = document.createElement('style')
    style.textContent = tokens
    document.head.appendChild(style)
    loaded = true
  }
  document.documentElement.dataset.theme = theme
}
export const BOTH: Theme[] = ['paper', 'terminal']
