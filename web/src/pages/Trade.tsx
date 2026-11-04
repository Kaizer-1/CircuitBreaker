// The phone: my account, one stock live, the order ticket, my open orders and holdings (J-03 to
// J-07; A-05, A-07, Q-02, Q-06, Q-07, Q-10). One layout for both skins; the terminal skin adds the
// command line and the F-key tabs at the bottom.
import { useCallback, useEffect, useState } from 'react'
import { CandleChart } from '../components/CandleChart'
import { Flash } from '../components/Flash'
import { Nickname } from '../components/Nickname'
import { OrderTicket } from '../components/OrderTicket'
import { ThemeToggle } from '../components/ThemeToggle'
import { arrow, direction, inr, percent, quantity, rupees, signedRupees } from '../lib/format'
import { ApiError, api, type Order, type OrderBody, type Ticker, TICKERS } from '../lib/market'
import { navigate } from '../lib/router'
import { loadTrader, saveTrader, type TraderSession } from '../lib/session'
import { useInstruments, useMarket, usePortfolio, type PortfolioEvent } from '../market/hooks'

export function Trade() {
  const [session] = useState<TraderSession | null>(loadTrader)
  useEffect(() => {
    if (!session) navigate('/join')
  }, [session])
  return session ? <Phone session={session} /> : null
}

function Phone({ session }: { session: TraderSession }) {
  const token = session.token
  const [ticker, setTicker] = useState<Ticker>('CHAIWALA')
  const [orders, setOrders] = useState<Order[]>([])
  const [toast, setToast] = useState<string | null>(null)
  const [view, stream] = useMarket(ticker, token)
  const instruments = useInstruments(token, 4000)
  const info = instruments.find((i) => i.ticker === ticker)

  const say = useCallback((text: string) => {
    setToast(text)
    window.setTimeout(() => setToast((t) => (t === text ? null : t)), 3500)
  }, [])
  const reload = useCallback(() => {
    api
      .openOrders(token)
      .then(setOrders)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) {
          saveTrader(null) // the 12-hour session ran out: join again
          navigate('/join')
        }
      })
  }, [token])
  useEffect(reload, [reload])

  const onEvent = useCallback(
    (event: PortfolioEvent) => {
      const d = event.data
      if (event.type === 'fill')
        say(
          `${d.side === 'BUY' ? 'Bought' : 'Sold'} ${d.quantity} ${d.ticker} at ${inr(Number(d.price_paise))}`,
        )
      if (
        event.type === 'order_closed' &&
        d.status === 'CANCELLED' &&
        d.cancel_reason === 'UNFILLED_MARKET_ORDER'
      )
        say('Part of your market order found no seller within 10% and was cancelled.')
      reload()
    },
    [reload, say],
  )
  const [portfolio, portfolioStream] = usePortfolio(token, onEvent)

  const last = view.lastPrice ?? info?.last_price_paise ?? null
  const change = last !== null && info ? last - info.reference_price_paise : 0
  const holding = portfolio?.holdings.find((h) => h.ticker === ticker)

  async function place(order: OrderBody) {
    try {
      await api.place(token, order, crypto.randomUUID())
      say(`Order sent: ${order.side} ${order.quantity} ${order.ticker}. Waiting for the exchange…`)
      reload()
    } catch (e) {
      say(e instanceof ApiError ? e.message : 'The order did not go through. Try again.')
    }
  }

  async function cancel(order: Order) {
    try {
      await api.cancel(token, order.order_id)
      say(`Cancelling ${order.side} ${order.quantity} ${order.ticker}…`)
      reload()
    } catch (e) {
      say(e instanceof ApiError ? e.message : 'Could not cancel. Try again.')
    }
  }

  return (
    <div className="phone">
      <header className="phone-head">
        <span className="masthead paper-only">
          CircuitBreaker <em>Markets</em>
        </span>
        <div className="command terminal-only">
          <span className="badge">CB</span>
          <span>
            &gt; <span className="typed">{ticker}</span> GP
            <span className="cursor" aria-hidden="true" />
          </span>
        </div>
        <ThemeToggle />
      </header>

      <section className="panel" id="account" aria-label="My account">
        <h2 className="terminal-only">
          <span>Account</span>
          <small>₹1,00,000 start</small>
        </h2>
        <div className="account">
          <div>
            <div className="label">
              <Nickname nickname={session.nickname} suffix={session.suffix} /> · net worth
            </div>
            <div className="worth num">{portfolio ? inr(portfolio.net_worth_paise) : '—'}</div>
          </div>
          {portfolio && (
            <div className={`pnl ${direction(portfolio.pnl_paise)}`}>
              {arrow(portfolio.pnl_paise)} {signedRupees(portfolio.pnl_paise)}
              <small>{percent(portfolio.pnl_percent)} since you joined</small>
            </div>
          )}
        </div>
        {portfolioStream === 'reconnecting' && (
          <div className="stream-note">Reconnecting to your portfolio…</div>
        )}
      </section>

      <section className="panel" id="market" aria-label={`${ticker} price`}>
        <h2 className="terminal-only">
          <span>
            <b>{ticker}</b> EQUITY
          </span>
          <small>5s</small>
        </h2>
        <div className="quote">
          <h1>
            {ticker}
            <small>{info?.name ?? ''}</small>
          </h1>
          <div>
            <div className="price num">
              <Flash value={last}>{last === null ? '—' : inr(last)}</Flash>
            </div>
            <div className={`change ${direction(change)}`}>
              {arrow(change)} {signedRupees(change)}{' '}
              {info ? percent((change * 100) / info.reference_price_paise) : ''}
            </div>
          </div>
        </div>
        <CandleChart candles={view.candles['5s'].slice(-48)} />
        {stream === 'reconnecting' && <div className="stream-note">Market data reconnecting…</div>}
        <div className="selector" role="group" aria-label="Choose a stock">
          {TICKERS.map((t) => {
            const i = instruments.find((x) => x.ticker === t)
            return (
              <button
                key={t}
                type="button"
                aria-pressed={t === ticker}
                onClick={() => setTicker(t)}
              >
                {t}
                <span className={t === ticker || !i ? '' : direction(i.change_paise)}>
                  {i ? `${arrow(i.change_paise)}${Math.abs(i.change_percent).toFixed(1)}%` : '—'}
                </span>
              </button>
            )
          })}
        </div>
      </section>

      <OrderTicket
        ticker={ticker}
        lastPricePaise={last}
        availableCashPaise={portfolio?.available_cash_paise ?? 0}
        availableShares={holding?.available_quantity ?? 0}
        onSubmit={place}
      />

      <section className="panel orders" id="orders" aria-label="Open orders">
        <h2>
          <span>Open orders</span>
          <small>{orders.length ? 'Tap to cancel' : ''}</small>
        </h2>
        {orders.length === 0 && <p className="empty">No open orders.</p>}
        {orders.map((o) => (
          <div className="order" key={o.order_id}>
            <div>
              <div className="what">
                <b className={o.side === 'BUY' ? 'up' : 'down'}>
                  {o.side === 'BUY' ? '▲' : '▼'} {o.side}
                </b>{' '}
                {o.quantity} {o.ticker}{' '}
                <span className="num">
                  {o.limit_price_paise ? `at ₹${rupees(o.limit_price_paise)}` : 'at market'}
                </span>
              </div>
              <div className="state">
                {o.cancel_requested
                  ? 'Cancelling'
                  : o.status === 'PARTIALLY_FILLED'
                    ? `Part filled · ${o.filled_quantity} of ${o.quantity}`
                    : o.status === 'PENDING'
                      ? 'Waiting for the exchange'
                      : 'Open'}
              </div>
            </div>
            <button
              type="button"
              className="cancel"
              disabled={o.cancel_requested}
              onClick={() => void cancel(o)}
            >
              <span className="paper-only">CANCEL</span>
              <span className="terminal-only">CXL</span>
            </button>
          </div>
        ))}
      </section>

      <section className="panel" id="holdings" aria-label="Holdings">
        <h2>
          <span>Holdings</span>
          <small>At last price</small>
        </h2>
        {portfolio && portfolio.holdings.length === 0 && (
          <p className="empty">No shares yet: buy some above.</p>
        )}
        {portfolio && portfolio.holdings.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Stock · qty @ avg ₹</th>
                <th>Value ₹</th>
                <th>P&amp;L ₹</th>
              </tr>
            </thead>
            <tbody>
              {portfolio.holdings.map((h) => (
                <tr key={h.ticker}>
                  {/* The average cost sits under the stock: five columns do not fit 390 px. */}
                  <td className="stock">
                    {h.ticker}
                    <small>
                      {quantity(h.quantity)} @ {rupees(h.avg_cost_paise)}
                    </small>
                  </td>
                  <td className="num">{rupees(h.market_value_paise)}</td>
                  <td className={`num ${direction(h.unrealised_pnl_paise)}`}>
                    {arrow(h.unrealised_pnl_paise)} {signedRupees(h.unrealised_pnl_paise)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <p className="attribution">
        Charts by{' '}
        <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">
          TradingView
        </a>
      </p>
      {toast && (
        <div className="toast" role="status">
          {toast}
        </div>
      )}
      <nav className="tabs terminal-only" aria-label="Sections">
        <a href="#market" className="on">
          <b>F1</b>MARKET
        </a>
        <a href="#ticket">
          <b>F2</b>TICKET
        </a>
        <a href="#orders">
          <b>F3</b>ORDERS
        </a>
        <a href="#holdings">
          <b>F4</b>HOLDINGS
        </a>
      </nav>
    </div>
  )
}
