import { chooseTheme, type Theme, useTheme } from '../theme/theme'

const NEXT: Record<Theme, Theme> = { paper: 'terminal', terminal: 'paper' }
const LABEL: Record<Theme, string> = { paper: 'Paper', terminal: 'Terminal' }

/** Switches between the light (paper) and dark (terminal) skins; remembered on this device. */
export function ThemeToggle() {
  const theme = useTheme()
  const next = NEXT[theme]
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={() => chooseTheme(next)}
      aria-label={`Switch to the ${LABEL[next]} theme`}
      title={`Switch to the ${LABEL[next]} theme`}
    >
      <span className="paper-only">Terminal</span>
      <span className="terminal-only">PAPER</span>
    </button>
  )
}
