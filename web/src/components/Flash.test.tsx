import { render, screen } from '@testing-library/react'
import { BOTH, showTheme } from '../../test/themes'
import { Flash } from './Flash'

describe.each(BOTH)('a flashing number (%s theme)', (theme) => {
  beforeEach(() => showTheme(theme))

  it('flashes up when it rises, down when it falls, and not on the first value', () => {
    const { rerender } = render(<Flash value={100}>1.00</Flash>)
    expect(screen.getByText('1.00').className).toBe('')
    rerender(<Flash value={105}>1.05</Flash>)
    expect(screen.getByText('1.05')).toHaveClass('flash-up')
    rerender(<Flash value={95}>0.95</Flash>)
    expect(screen.getByText('0.95')).toHaveClass('flash-down')
    rerender(<Flash value={95}>0.95</Flash>)
    expect(screen.getByText('0.95')).toHaveClass('flash-down') // unchanged: no new flash
  })

  it('flashes with the theme’s own timing', () => {
    expect(getComputedStyle(document.documentElement).getPropertyValue('--flash-ms')).toBe(
      theme === 'paper' ? '900ms' : '600ms',
    )
  })
})
