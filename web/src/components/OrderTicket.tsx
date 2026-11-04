// A-05 from the phone: BUY or SELL, LIMIT or MARKET, quantity and price with big − / + buttons.
// Checked here with the exchange's rules (lib/orderForm.ts) so a trader rarely sees a refusal.
import { type FormEvent, useState } from 'react'
import { inr, rupees } from '../lib/format'
import type { OrderBody, OrderType, Side, Ticker } from '../lib/market'
import { checkOrder, stepPrice } from '../lib/orderForm'

export interface TicketProps {
  ticker: Ticker
  lastPricePaise: number | null
  availableCashPaise: number
  availableShares: number
  onSubmit: (order: OrderBody) => Promise<void>
}

export function OrderTicket({
  ticker,
  lastPricePaise,
  availableCashPaise,
  availableShares,
  onSubmit,
}: TicketProps) {
  const [side, setSide] = useState<Side>('BUY')
  const [type, setType] = useState<OrderType>('LIMIT')
  const [qty, setQty] = useState('10')
  const [price, setPrice] = useState(lastPricePaise ? (lastPricePaise / 100).toFixed(2) : '')
  const [touched, setTouched] = useState(false)
  const [busy, setBusy] = useState(false)
  const [seededFor, setSeededFor] = useState<Ticker | null>(lastPricePaise ? ticker : null)
  if (seededFor !== ticker && lastPricePaise) {
    // A new stock (or its first price arrived): start the limit price at the last trade.
    setSeededFor(ticker)
    setPrice((lastPricePaise / 100).toFixed(2))
  }

  const checked = checkOrder(
    { ticker, side, type, quantity: qty, price },
    { availableCashPaise, availableShares, lastPricePaise },
  )
  const show = touched ? checked.errors : {}

  async function submit(event: FormEvent) {
    event.preventDefault()
    setTouched(true)
    if (!checked.order) return
    setBusy(true)
    try {
      await onSubmit(checked.order)
    } finally {
      setBusy(false)
    }
  }

  const quantityNumber = Number(qty) || 0
  const summary =
    type === 'LIMIT'
      ? `${side} ${quantityNumber} ${ticker} at ₹${price || '—'}`
      : `${side} ${quantityNumber} ${ticker} at market`

  return (
    <form
      className="panel ticket"
      id="ticket"
      onSubmit={(e) => void submit(e)}
      aria-label="Order ticket"
      noValidate
    >
      <h2>
        <span>Place an order</span>
        <small className="terminal-only">AVAIL {rupees(availableCashPaise)}</small>
      </h2>
      <div className="seg" role="group" aria-label="Buy or sell">
        <button
          type="button"
          className="buy"
          aria-pressed={side === 'BUY'}
          onClick={() => setSide('BUY')}
        >
          ▲ BUY
        </button>
        <button
          type="button"
          className="sell"
          aria-pressed={side === 'SELL'}
          onClick={() => setSide('SELL')}
        >
          ▼ SELL
        </button>
      </div>
      <div className="seg" role="group" aria-label="Order type">
        <button
          type="button"
          className="kind"
          aria-pressed={type === 'LIMIT'}
          onClick={() => setType('LIMIT')}
        >
          LIMIT
        </button>
        <button
          type="button"
          className="kind"
          aria-pressed={type === 'MARKET'}
          onClick={() => setType('MARKET')}
        >
          MARKET
        </button>
      </div>
      <div className="fields">
        <div className="field">
          <label htmlFor="qty">Quantity</label>
          <div className="stepper">
            <button
              type="button"
              aria-label="One share less"
              onClick={() => setQty(String(Math.max(1, quantityNumber - 1)))}
            >
              −
            </button>
            <input
              id="qty"
              inputMode="numeric"
              value={qty}
              onChange={(e) => setQty(e.target.value)}
              aria-invalid={Boolean(show.quantity)}
              aria-describedby={show.quantity ? 'qty-error' : undefined}
            />
            <button
              type="button"
              aria-label="One share more"
              onClick={() => setQty(String(quantityNumber + 1))}
            >
              +
            </button>
          </div>
          {show.quantity && (
            <p className="error" id="qty-error" role="alert">
              {show.quantity}
            </p>
          )}
        </div>
        <div className="field">
          <label htmlFor="price">{type === 'LIMIT' ? 'Limit price ₹' : 'Price'}</label>
          <div className="stepper">
            <button
              type="button"
              aria-label="Five paise lower"
              disabled={type === 'MARKET'}
              onClick={() => setPrice(stepPrice(price, -1))}
            >
              −
            </button>
            <input
              id="price"
              inputMode="decimal"
              value={type === 'LIMIT' ? price : 'MARKET'}
              disabled={type === 'MARKET'}
              onChange={(e) => setPrice(e.target.value)}
              aria-invalid={Boolean(show.price)}
              aria-describedby={show.price ? 'price-error' : undefined}
            />
            <button
              type="button"
              aria-label="Five paise higher"
              disabled={type === 'MARKET'}
              onClick={() => setPrice(stepPrice(price, 1))}
            >
              +
            </button>
          </div>
          {show.price && (
            <p className="error" id="price-error" role="alert">
              {show.price}
            </p>
          )}
        </div>
      </div>
      <div className="estimate">
        <span>{side === 'BUY' ? 'Sets aside' : 'Shares you can sell'}</span>
        <b className="num">{side === 'BUY' ? inr(checked.reservePaise) : availableShares}</b>
      </div>
      <div className="estimate paper-only">
        <span>Available cash</span>
        <b className="num">{inr(availableCashPaise)}</b>
      </div>
      {show.funds && (
        <p className="error" role="alert">
          {show.funds}
        </p>
      )}
      <button type="submit" className={`submit ${side === 'BUY' ? 'buy' : 'sell'}`} disabled={busy}>
        <span className="terminal-only">&lt;GO&gt; </span>
        {busy ? 'Sending…' : summary.toUpperCase()}
      </button>
    </form>
  )
}
