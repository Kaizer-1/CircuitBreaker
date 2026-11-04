import { fireEvent, render, screen } from '@testing-library/react'
import { BOTH, showTheme } from '../../test/themes'
import { ThemeToggle } from './ThemeToggle'

describe.each(BOTH)('the theme toggle (from %s)', (theme) => {
  beforeEach(() => showTheme(theme))

  it('switches to the other skin and remembers it on this device', () => {
    render(<ThemeToggle />)
    const other = theme === 'paper' ? 'terminal' : 'paper'
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`Switch to the ${other}`, 'i') }))
    expect(document.documentElement.dataset.theme).toBe(other)
    expect(localStorage.getItem('circuitbreaker.theme')).toBe(other)
  })
})
