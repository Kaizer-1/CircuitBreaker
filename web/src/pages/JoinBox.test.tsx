import { render, screen } from '@testing-library/react'
import { LOCALHOST_WARNING } from '../lib/config'
import { BOTH, showTheme } from '../../test/themes'
import { JoinBox } from './Dashboard'

describe.each(BOTH)('the presenter’s join box (%s theme)', (theme) => {
  beforeEach(() => showTheme(theme))
  afterEach(() => {
    window.CB_CONFIG = undefined
  })

  it('warns that phones cannot join when the QR code points to localhost', () => {
    window.CB_CONFIG = { publicBaseUrl: 'http://localhost:5173' }
    render(<JoinBox />)
    expect(screen.getByRole('alert')).toHaveTextContent(LOCALHOST_WARNING)
    expect(screen.getByText(/localhost:5173/).textContent).toBe('localhost:5173/join')
  })

  it('shows the join address and no warning for a LAN address', () => {
    window.CB_CONFIG = { publicBaseUrl: 'http://192.168.43.12:5173' }
    render(<JoinBox />)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText(/192\.168/).textContent).toBe('192.168.43.12:5173/join') // as typed
    expect(screen.getByText(/No password/)).toBeInTheDocument()
  })
})
