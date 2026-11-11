// The scrubber: where the handle is, the keys, the markers, and "now" at the right edge.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { Candle } from '../lib/market'
import { BOTH, showTheme } from '../../test/themes'
import { Timeline } from './Timeline'

const START = Date.parse('2026-11-20T10:00:00.000Z')
const END = START + 60 * 60_000
const candles: Candle[] = Array.from({ length: 60 }, (_, n) => ({
  bucket_start: new Date(START + n * 60_000).toISOString(),
  open_paise: 24500,
  high_paise: 24600,
  low_paise: 24400,
  close_paise: 24500 + (n % 7) * 10,
  volume: 100,
  trade_count: 5,
}))

function setup(position: number | null) {
  const onSeek = vi.fn()
  const onSeekNow = vi.fn()
  render(
    <Timeline
      candles={candles}
      start={START}
      end={END}
      position={position}
      moments={[{ at: START + 30 * 60_000, change: -310 }]}
      onSeek={onSeek}
      onSeekNow={onSeekNow}
    />,
  )
  return { onSeek, onSeekNow, handle: screen.getByRole('slider', { name: 'Moment shown' }) }
}

describe.each(BOTH)('the timeline (%s theme)', (theme) => {
  beforeEach(() => showTheme(theme))
  afterEach(cleanup)

  it('live: the handle is at the right edge and says now', () => {
    const { handle } = setup(null)
    expect(handle).toHaveAttribute('aria-valuetext', 'now, live')
    expect(handle.style.left).toBe('100%')
    expect(screen.getByText('NOW')).toBeInTheDocument()
  })

  it('in the past: the handle sits at the moment, and the keys move it', () => {
    const at = START + 20 * 60_000
    const { handle, onSeek, onSeekNow } = setup(at)
    expect(handle.style.left).toBe(`${(20 / 60) * 100}%`)
    fireEvent.keyDown(handle, { key: 'ArrowLeft' })
    fireEvent.keyDown(handle, { key: 'ArrowRight', shiftKey: true })
    fireEvent.keyDown(handle, { key: 'PageDown' })
    fireEvent.keyDown(handle, { key: 'Home' })
    expect(onSeek.mock.calls.map(([t]) => t)).toEqual([
      at - 60_000,
      at + 10_000,
      at - 600_000,
      START,
    ])
    fireEvent.keyDown(handle, { key: 'End' })
    expect(onSeekNow).toHaveBeenCalledTimes(1)
  })

  it('a marker jumps to its minute and says what happened there', () => {
    const { onSeek } = setup(null)
    const marker = screen.getByRole('button', { name: /−3\.10 in a minute/ })
    expect(marker).toHaveTextContent('▼')
    fireEvent.click(marker)
    expect(onSeek).toHaveBeenCalledWith(START + 30 * 60_000)
  })
})
