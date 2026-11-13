// The terminal skin's F-key tabs: a tap scrolls its section to the top without leaving the page
// (the URL keeps no #hash), and the lit tab follows the scroll. The paper skin has no tab bar: its
// page is read top to bottom like a newspaper.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { showTheme } from '../../test/themes'
import { type Section, SectionTabs } from './SectionTabs'

const SECTIONS: Section[] = [
  { id: 'market', key: 'F1', label: 'MARKET' },
  { id: 'ticket', key: 'F2', label: 'TICKET' },
  { id: 'orders', key: 'F3', label: 'ORDERS' },
  { id: 'holdings', key: 'F4', label: 'HOLDINGS' },
]
const HEIGHT = 400 // each section's height on the fake page

/** The page scrolled to `y`: each section's top on screen, as the browser would report it. */
function scrollTo(y: number) {
  SECTIONS.forEach((s, i) => {
    const element = document.getElementById(s.id)!
    element.getBoundingClientRect = () => ({ top: i * HEIGHT - y }) as DOMRect
  })
  act(() => {
    window.dispatchEvent(new Event('scroll'))
  })
}
const lit = () => screen.getAllByRole('link').filter((a) => a.classList.contains('on'))

function page() {
  const scrolled: string[] = []
  render(
    <>
      {SECTIONS.map((s) => (
        <section key={s.id} id={s.id} />
      ))}
      <SectionTabs sections={SECTIONS} />
    </>,
  )
  for (const s of SECTIONS)
    document.getElementById(s.id)!.scrollIntoView = () => {
      scrolled.push(s.id)
    }
  return scrolled
}

afterEach(cleanup)

describe('the F-key tabs (terminal theme)', () => {
  beforeEach(() => {
    showTheme('terminal')
    window.history.replaceState({}, '', '/trade')
  })

  it('a tap scrolls to its section, lights its tab, and stays on /trade', () => {
    const scrolled = page()
    for (const s of SECTIONS) {
      const tab = screen.getByRole('link', { name: `${s.key}${s.label}` })
      const notPrevented = fireEvent.click(tab)
      expect(notPrevented).toBe(false) // the browser's own #hash jump is cancelled
      expect(scrolled.at(-1)).toBe(s.id)
      expect(lit()).toEqual([tab])
      expect(tab).toHaveAttribute('aria-current', 'location')
    }
    expect(window.location.pathname + window.location.hash).toBe('/trade')
  })

  it('the lit tab follows the scroll', () => {
    page()
    scrollTo(0)
    expect(lit()[0]).toHaveTextContent('F1MARKET')
    scrollTo(HEIGHT * 2 - 10) // ORDERS' top is 10 px down: at the top
    expect(lit()[0]).toHaveTextContent('F3ORDERS')
    scrollTo(HEIGHT * 3)
    expect(lit()[0]).toHaveTextContent('F4HOLDINGS')
    scrollTo(HEIGHT - 40) // TICKET's top is 40 px down: not there yet
    expect(lit()[0]).toHaveTextContent('F1MARKET')
    expect(lit()).toHaveLength(1)
  })
})

it('the paper theme shows no tab bar', () => {
  showTheme('paper')
  page()
  expect(screen.queryByRole('navigation', { name: 'Sections' })).toBeNull()
})
