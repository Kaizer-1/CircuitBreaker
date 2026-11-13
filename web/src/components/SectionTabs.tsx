// The terminal skin's F-key tabs at the bottom of the phone (docs/design/visual-direction.md): shortcuts
// to the sections of the one long trading page. A tap scrolls its section to the top of the screen
// (no reload, the URL stays /trade), and the lit tab follows the scroll: the last section that has
// reached the top of the screen, where a tap puts it (its scroll-margin-top, below the iPhone's
// status bar). A fixed reading line lower down would light the wrong tab under a short section,
// such as "No open orders.".
import { type MouseEvent, useEffect, useState } from 'react'

export interface Section {
  id: string
  key: string
  label: string
}

const SLACK = 16 // px below the spot a tap scrolls to that still counts as "at the top"

/** The id of the section being read: the last one scrolled to the top of the screen. */
export function sectionInView(ids: string[]): string {
  let current = ids[0]
  for (const id of ids) {
    const element = document.getElementById(id)
    if (!element) continue
    const margin = parseFloat(getComputedStyle(element).scrollMarginTop) || 0
    if (element.getBoundingClientRect().top <= margin + SLACK) current = id
  }
  return current
}

export function SectionTabs({ sections }: { sections: Section[] }) {
  const ids = sections.map((s) => s.id)
  const key = ids.join(' ')
  const [active, setActive] = useState(ids[0])

  useEffect(() => {
    const list = key.split(' ')
    const update = () => setActive(sectionInView(list))
    update()
    window.addEventListener('scroll', update, { passive: true })
    window.addEventListener('resize', update)
    return () => {
      window.removeEventListener('scroll', update)
      window.removeEventListener('resize', update)
    }
  }, [key])

  function go(event: MouseEvent, id: string) {
    event.preventDefault() // a plain #hash jump changes the URL and does not move a short page
    setActive(id)
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <nav className="tabs terminal-only" aria-label="Sections">
      {sections.map((s) => (
        <a
          key={s.id}
          href={`#${s.id}`}
          className={s.id === active ? 'on' : undefined}
          aria-current={s.id === active ? 'location' : undefined}
          onClick={(e) => go(e, s.id)}
        >
          <b>{s.key}</b>
          {s.label}
        </a>
      ))}
    </nav>
  )
}
