import { fireEvent, render, screen } from '@testing-library/react'
import appCss from '../theme/app.css?raw'
import tokensCss from '../theme/tokens.css?raw'
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

  it('goes back to its normal colour when the flash ends, and flashes again on the next tick', () => {
    const { rerender } = render(<Flash value={100}>1.00</Flash>)
    rerender(<Flash value={105}>1.05</Flash>)
    fireEvent.animationEnd(screen.getByText('1.05'))
    expect(screen.getByText('1.05').className).toBe('') // neutral: no flash colour left behind
    rerender(<Flash value={110}>1.10</Flash>)
    expect(screen.getByText('1.10')).toHaveClass('flash-up')
  })

  it('ignores a child’s animation ending (it is not the flash)', () => {
    const { rerender } = render(
      <Flash value={100}>
        <b>1.00</b>
      </Flash>,
    )
    rerender(
      <Flash value={105}>
        <b>1.05</b>
      </Flash>,
    )
    fireEvent.animationEnd(screen.getByText('1.05'))
    expect(screen.getByText('1.05').parentElement).toHaveClass('flash-up')
  })

  it('flashes with the theme’s own timing', () => {
    expect(getComputedStyle(document.documentElement).getPropertyValue('--flash-ms')).toBe(
      theme === 'paper' ? '900ms' : '600ms',
    )
  })
})

describe('the flash animations', () => {
  // Only a `from` frame and no fill-mode: each animation ends on the element's own colour.
  it.each([
    ['flash', tokensCss],
    ['flash-ink', appCss],
  ])('%s starts in the flash colour and ends in the normal one', (name, css) => {
    const body = css.match(new RegExp(`@keyframes ${name} \\{([^@]*?)\\n\\}`))?.[1] ?? ''
    expect(body).toMatch(/from \{/)
    expect(body).not.toMatch(/\bto \{|100%/)
    expect(css).not.toMatch(/animation-fill-mode|forwards|both;/)
  })
})
