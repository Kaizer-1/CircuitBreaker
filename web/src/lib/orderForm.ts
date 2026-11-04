// The order ticket's rules, checked on the phone before anything is sent, with the same rules the
// exchange applies (docs/00-overview.md section 4; A-05, A-06): whole shares from 1 to 10,000;
// prices in steps of 5 paise; no margin (a buy needs the cash); no short selling (a sell needs the
// shares). The exchange checks again: this only spares the trader a refused order.
import type { OrderBody, OrderType, Side, Ticker } from './market'

export const TICK_PAISE = 5
export const MAX_QUANTITY = 10_000

export interface Draft {
  ticker: Ticker
  side: Side
  type: OrderType
  quantity: string
  price: string // rupees as typed, e.g. "247.80"
}

export interface Context {
  availableCashPaise: number
  availableShares: number
  lastPricePaise: number | null
}

export interface Checked {
  order: OrderBody | null
  errors: Partial<Record<'quantity' | 'price' | 'funds', string>>
  reservePaise: number // what the exchange will set aside
}

/** "247.80" → 24780; null if it is not a rupee amount with at most two decimals. */
export function parseRupees(text: string): number | null {
  const t = text.trim().replace(/,/g, '').replace(/^₹/, '')
  if (!/^\d+(\.\d{1,2})?$/.test(t)) return null
  const [whole, frac = ''] = t.split('.')
  return Number(whole) * 100 + Number((frac + '00').slice(0, 2))
}

/** What a MARKET buy reserves per share: last price + 10%, rounded up to the tick (A-06). */
export function protectionPaise(lastPricePaise: number): number {
  return Math.ceil((lastPricePaise * 1.1) / TICK_PAISE) * TICK_PAISE
}

export function checkOrder(draft: Draft, ctx: Context): Checked {
  const errors: Checked['errors'] = {}
  const qtyText = draft.quantity.trim()
  const quantity = /^\d+$/.test(qtyText) ? Number(qtyText) : NaN
  if (!Number.isInteger(quantity) || quantity < 1)
    errors.quantity = 'Enter a whole number of shares, at least 1.'
  else if (quantity > MAX_QUANTITY) errors.quantity = 'At most 10,000 shares in one order.'

  let price: number | null = null
  if (draft.type === 'LIMIT') {
    price = parseRupees(draft.price)
    if (price === null || price <= 0) errors.price = 'Enter a price in rupees, like 247.80.'
    else if (price % TICK_PAISE !== 0) errors.price = 'Prices move in steps of ₹0.05.'
  }

  let reserve = 0
  if (!errors.quantity && !errors.price) {
    if (draft.side === 'BUY') {
      const per =
        draft.type === 'LIMIT'
          ? (price as number)
          : ctx.lastPricePaise
            ? protectionPaise(ctx.lastPricePaise)
            : null
      if (per === null)
        errors.price = 'No price yet: wait for the first trade or use a limit price.'
      else {
        reserve = quantity * per
        if (reserve > ctx.availableCashPaise)
          errors.funds = 'Not enough cash for this order (no borrowing).'
      }
    } else if (quantity > ctx.availableShares) {
      errors.funds =
        ctx.availableShares === 0
          ? 'You have no shares of this stock to sell (no short selling).'
          : `You can sell at most ${ctx.availableShares} (no short selling).`
    }
  }

  const ok = Object.keys(errors).length === 0
  return {
    errors,
    reservePaise: reserve,
    order: ok
      ? {
          ticker: draft.ticker,
          side: draft.side,
          order_type: draft.type,
          quantity,
          ...(draft.type === 'LIMIT' ? { limit_price_paise: price as number } : {}),
        }
      : null,
  }
}

/** The price field's − and + buttons: one tick down or up, never below one tick. */
export function stepPrice(text: string, steps: number): string {
  const paise = parseRupees(text) ?? 0
  const next = Math.max(
    TICK_PAISE,
    Math.round(paise / TICK_PAISE) * TICK_PAISE + steps * TICK_PAISE,
  )
  return (next / 100).toFixed(2)
}
