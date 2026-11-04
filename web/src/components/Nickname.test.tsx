import { render, screen } from '@testing-library/react'
import { BOTH, showTheme } from '../../test/themes'
import { Nickname } from './Nickname'

describe.each(BOTH)('a nickname (%s theme)', (theme) => {
  beforeEach(() => showTheme(theme))

  it('is shown exactly as typed', () => {
    const { container } = render(<Nickname nickname="RajuTrader" />)
    expect(container.textContent).toBe('RajuTrader')
    expect(container.querySelector('.suffix')).toBeNull()
  })

  it('shows a suffix added for a duplicate apart, so it can be quieter', () => {
    render(<Nickname nickname="RajuTrader_7k" suffix="_7k" />)
    const suffix = screen.getByText('_7k')
    expect(suffix).toHaveClass('suffix')
    expect(suffix.parentElement?.firstChild?.textContent).toBe('RajuTrader')
    expect(suffix.parentElement).toHaveAttribute('title', 'RajuTrader was taken, so _7k was added')
  })
})
