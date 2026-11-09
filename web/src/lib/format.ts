// Money and numbers. Prices are integer paise everywhere (ADR-0012); this is the only place they
// become text. Indian digit grouping (1,00,000.00) from the en-IN locale; a real minus sign (−).

const RUPEES = new Intl.NumberFormat('en-IN', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})
const WHOLE = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 })
const MINUS = '−'

/** 10007300 → "1,00,073.00" (no symbol, for tables where the column says ₹). */
export function rupees(paise: number): string {
  const text = RUPEES.format(Math.abs(paise) / 100)
  return paise < 0 ? MINUS + text : text
}

/** 10007300 → "₹1,00,073.00". */
export function inr(paise: number): string {
  return (paise < 0 ? MINUS : '') + '₹' + RUPEES.format(Math.abs(paise) / 100)
}

/** A change with its sign: +2.35, −1.90, ±0.00. */
export function signedRupees(paise: number): string {
  const text = RUPEES.format(Math.abs(paise) / 100)
  return (paise > 0 ? '+' : paise < 0 ? MINUS : '±') + text
}

/** 0.957 → "+0.96%". */
export function percent(value: number): string {
  const text = Math.abs(value).toFixed(2) + '%'
  return (value > 0 ? '+' : value < 0 ? MINUS : '±') + text
}

/** 53623 → "53,623"; 100000 → "1,00,000". */
export function quantity(n: number): string {
  return WHOLE.format(n)
}

/** ▲ for up, ▼ for down, nothing for unchanged: the non-colour cue next to every change. */
export function arrow(change: number): string {
  return change > 0 ? '▲' : change < 0 ? '▼' : ''
}

export function direction(change: number): 'up' | 'down' | 'flat' {
  return change > 0 ? 'up' : change < 0 ? 'down' : 'flat'
}

/** "15:42:31" in India Standard Time. */
export function clock(iso: string | Date): string {
  const at = typeof iso === 'string' ? new Date(iso) : iso
  return at.toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour12: false })
}

/** "Friday 20 November 2026" (the paper theme's dateline). */
export function dateline(at: Date): string {
  const zone = { timeZone: 'Asia/Kolkata' } as const
  const weekday = at.toLocaleDateString('en-GB', { ...zone, weekday: 'long' })
  const date = at.toLocaleDateString('en-GB', {
    ...zone,
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  })
  return `${weekday} ${date}`
}

/** "09 NOV 2026" (the terminal skin's date). */
export function shortDate(at: Date): string {
  return at
    .toLocaleDateString('en-GB', {
      timeZone: 'Asia/Kolkata',
      day: '2-digit',
      month: 'short',
      year: 'numeric',
    })
    .toUpperCase()
}
