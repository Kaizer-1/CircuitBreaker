import { type AnimationEvent, type ReactNode, useState } from 'react'

/**
 * A number that flashes when it changes: green when it rose, red when it fell, in the theme's way
 * (paper: fresh ink that dries; terminal: the cell lights). The motion means "this just changed",
 * so when the animation ends the flash class goes and the number is back in its normal colour.
 */
export function Flash({
  value,
  children,
  className = '',
}: {
  value: number | null
  children: ReactNode
  className?: string
}) {
  const [prev, setPrev] = useState(value)
  const [flash, setFlash] = useState<{ dir: 'up' | 'down' | null; n: number }>({ dir: null, n: 0 })
  if (value !== prev) {
    // Adjusting state while rendering when a prop changes (react.dev "You might not need an effect").
    setPrev(value)
    if (value !== null && prev !== null)
      setFlash((f) => ({ dir: value > prev ? 'up' : 'down', n: f.n + 1 }))
  }
  const settle = (e: AnimationEvent) => {
    if (e.target === e.currentTarget) setFlash((f) => ({ dir: null, n: f.n }))
  }
  return (
    <span
      key={flash.n}
      className={`${className} ${flash.dir ? `flash-${flash.dir}` : ''}`.trim()}
      onAnimationEnd={settle}
    >
      {children}
    </span>
  )
}
