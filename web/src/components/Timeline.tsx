// The time-travel scrubber: the session's one-minute closes as a line, markers at its biggest
// moves, and a handle at the moment shown (at the right edge, "now", when live). Drag or click to
// seek; a marker jumps to its minute. Keyboard (the handle is a slider): ← → one minute, Shift
// with them ten seconds, Page Up/Down ten minutes, Home the start, End now.
import { type KeyboardEvent, type PointerEvent, useRef, useState } from 'react'
import { arrow, clock, direction, signedRupees } from '../lib/format'
import type { Candle } from '../lib/market'
import type { Moment } from '../timetravel/state'

const NEAR_END = 0.005 // released this close to the right edge: "now"

/** At `ratio` of the track, anchored so it never sticks out: its left edge at the start, its
 * centre in the middle, its right edge at the end. */
const place = (ratio: number) => ({
  left: `${ratio * 100}%`,
  transform: `translateX(-${ratio * 100}%)`,
})

interface Props {
  candles: Candle[] // one-minute, oldest first
  start: number
  end: number // now
  position: number | null // null: live, at the right edge
  moments: Moment[]
  onSeek: (at: number) => void
  onSeekNow: () => void
}

export function Timeline({ candles, start, end, position, moments, onSeek, onSeekNow }: Props) {
  const track = useRef<HTMLDivElement>(null)
  const [preview, setPreview] = useState<number | null>(null) // while dragging
  const span = Math.max(1, end - start)
  const x = (at: number) => Math.min(1, Math.max(0, (at - start) / span))
  const shown = preview ?? position ?? end
  const live = preview === null && position === null

  const closes = candles.map((c) => c.close_paise)
  const low = Math.min(...closes)
  const high = Math.max(...closes)
  const y = (p: number) => (high === low ? 50 : 90 - ((p - low) / (high - low)) * 80)
  const line = candles
    .map(
      (c, n) =>
        `${n ? 'L' : 'M'}${(x(Date.parse(c.bucket_start)) * 1000).toFixed(1)},${y(c.close_paise).toFixed(1)}`,
    )
    .join(' ')

  const atPointer = (e: PointerEvent) => {
    const box = track.current?.getBoundingClientRect()
    if (!box || box.width === 0) return { at: end, ratio: 1 }
    const ratio = Math.min(1, Math.max(0, (e.clientX - box.left) / box.width))
    return { at: Math.round(start + ratio * span), ratio }
  }
  const release = (e: PointerEvent) => {
    if (preview === null) return
    const { at, ratio } = atPointer(e)
    setPreview(null)
    if (ratio >= 1 - NEAR_END) onSeekNow()
    else onSeek(at)
  }
  const onKey = (e: KeyboardEvent) => {
    const from = position ?? end
    const step = e.shiftKey ? 10_000 : 60_000
    const to: Record<string, number | 'now'> = {
      ArrowLeft: from - step,
      ArrowRight: from + step,
      PageDown: from - 600_000,
      PageUp: from + 600_000,
      Home: start,
      End: 'now',
    }
    const target = to[e.key]
    if (target === undefined) return
    e.preventDefault()
    if (target === 'now' || target >= end) onSeekNow()
    else onSeek(Math.max(start, target))
  }

  return (
    <div className="timeline">
      <div
        className="track"
        ref={track}
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture?.(e.pointerId)
          setPreview(atPointer(e).at)
        }}
        onPointerMove={(e) => preview !== null && setPreview(atPointer(e).at)}
        onPointerUp={release}
        onPointerCancel={() => setPreview(null)}
      >
        <svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true">
          {line && <path d={line} vectorEffect="non-scaling-stroke" />}
        </svg>
        {moments.map((m) => (
          <button
            key={m.at}
            type="button"
            className={`moment ${direction(m.change)}`}
            style={place(x(m.at))}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={() => onSeek(m.at)}
            aria-label={`Jump to ${clock(new Date(m.at))}: ${signedRupees(m.change)} in a minute`}
            title={`${clock(new Date(m.at))} ${signedRupees(m.change)}`}
          >
            {arrow(m.change)}
          </button>
        ))}
        <div
          className="handle"
          style={place(x(shown))}
          role="slider"
          tabIndex={0}
          aria-label="Moment shown"
          aria-valuemin={start}
          aria-valuemax={end}
          aria-valuenow={shown}
          aria-valuetext={live ? 'now, live' : `${clock(new Date(shown))} IST`}
          onKeyDown={onKey}
        />
        <span className="handle-label" style={place(x(shown))} aria-hidden="true">
          {live ? 'NOW' : clock(new Date(shown))}
        </span>
      </div>
      <div className="ends">
        <span>{clock(new Date(start))}</span>
        <span>now {clock(new Date(end))}</span>
      </div>
    </div>
  )
}
