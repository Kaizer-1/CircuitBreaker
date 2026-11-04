// The presenter's projector view (web/README.md "Presenter"; admin only): every ticker live, the
// selected one big, its depth and trades, and the QR code to join. One layout for both skins; the
// masthead and dateline (paper) and the command line and F-keys (terminal) are theme-only details.
import { useCallback, useEffect, useState } from 'react'
import { fetchSystemStatus, NotAuthorizedError } from '../api'
import { AdminLogin } from '../components/AdminLogin'
import { CandleChart } from '../components/CandleChart'
import { Flash } from '../components/Flash'
import { QrCode } from '../components/QrCode'
import { ThemeToggle } from '../components/ThemeToggle'
import { joinUrl } from '../lib/config'
import {
  arrow,
  clock,
  dateline,
  direction,
  percent,
  quantity,
  rupees,
  signedRupees,
} from '../lib/format'
import { type Instrument, type Ticker, TICKERS } from '../lib/market'
import { loadAdminToken, saveAdminToken } from '../lib/session'
import { useInstruments, useMarkets } from '../market/hooks'

const DEPTH_ROWS = 6
const TAPE_ROWS = 9

function useNow(everyMs = 1000): Date {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), everyMs)
    return () => window.clearInterval(id)
  }, [everyMs])
  return now
}

function useHealth(token: string, onExpired: () => void): string {
  const [health, setHealth] = useState('checking')
  useEffect(() => {
    let stop = false
    const check = () =>
      fetchSystemStatus(token)
        .then((s) => {
          const bad = s.services.filter((x) => x.status !== 'ready').length
          if (!stop) setHealth(bad === 0 ? 'all systems healthy' : `${bad} services need attention`)
        })
        .catch((e) => {
          if (e instanceof NotAuthorizedError) onExpired()
          else if (!stop) setHealth('gateway unreachable')
        })
    void check()
    const id = window.setInterval(check, 10_000)
    return () => {
      stop = true
      window.clearInterval(id)
    }
  }, [token, onExpired])
  return health
}

export function Dashboard() {
  const [token, setToken] = useState<string | null>(loadAdminToken)
  const expired = useCallback(() => {
    saveAdminToken(null)
    setToken(null)
  }, [])
  if (!token)
    return (
      <main className="gate">
        <h1 className="masthead">
          CircuitBreaker <em>Markets</em>
        </h1>
        <AdminLogin
          purpose="The projector dashboard is for the presenter. Sign in with the presenter passcode."
          onToken={(t) => {
            saveAdminToken(t)
            setToken(t)
          }}
        />
      </main>
    )
  return <Board token={token} onExpired={expired} />
}

function Board({ token, onExpired }: { token: string; onExpired: () => void }) {
  const [selected, setSelected] = useState<Ticker>('CHAIWALA')
  const markets = useMarkets(token)
  const instruments = useInstruments(token)
  const byTicker = Object.fromEntries(instruments.map((i) => [i.ticker, i])) as Partial<
    Record<Ticker, Instrument>
  >
  const now = useNow()
  const health = useHealth(token, onExpired)

  // Keys 1 to 5 pick the ticker shown big (the presenter's remote clicker can send them).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const n = Number(e.key)
      if (n >= 1 && n <= TICKERS.length) setSelected(TICKERS[n - 1])
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const [view, state] = markets[selected]
  const info = byTicker[selected]
  const last = view.lastPrice ?? info?.last_price_paise ?? null
  const change = last !== null && info ? last - info.reference_price_paise : 0
  const live = TICKERS.filter((t) => markets[t][1] === 'live').length
  const volume = instruments.reduce((s, i) => s + i.day_volume, 0)
  const book = view.book
  const maxQty = Math.max(
    1,
    ...(book ? [...book.bids, ...book.asks].slice(0, 99).map((l) => l.quantity) : [1]),
  )

  return (
    <div className="dash">
      <header className="dash-head">
        <h1 className="masthead paper-only">
          CircuitBreaker <em>Markets</em>
        </h1>
        <span className="dateline paper-only">{dateline(now)} · live from the classroom floor</span>
        <div className="command terminal-only" aria-label="Command line">
          <span className="badge">CIRCUITBREAKER</span>
          <span>
            &gt; <span className="typed">{selected}</span> EQUITY GP
            <span className="cursor" aria-hidden="true" />
          </span>
        </div>
        <div className="right">
          <span>
            Market open · <b>{clock(now)}</b> IST
          </span>
          <ThemeToggle />
        </div>
      </header>
      <nav className="fkeys terminal-only" aria-label="Function keys">
        <span className="on">
          <b>F1</b>MARKET
        </span>
        <span>
          <b>F2</b>DEPTH
        </span>
        <span>
          <b>F3</b>TRADES
        </span>
        <span>
          <b>F8</b>JOIN
        </span>
        <a href="/status">
          <b>F12</b>SYSTEM
        </a>
      </nav>

      <section className="panel watch" aria-label="Watchlist">
        <h2 className="terminal-only">
          <span>
            Watchlist · <b>{TICKERS.length}</b> instruments
          </span>
          <small>Keys 1–5 choose the chart</small>
        </h2>
        <table>
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Name</th>
              <th>Last ₹</th>
              <th>Change</th>
              <th>Change %</th>
              <th>Bid</th>
              <th>Ask</th>
              <th>High</th>
              <th>Low</th>
              <th>Volume</th>
            </tr>
          </thead>
          <tbody>
            {TICKERS.map((t, n) => {
              const [m] = markets[t]
              const i = byTicker[t]
              const px = m.lastPrice ?? i?.last_price_paise ?? null
              const ch = px !== null && i ? px - i.reference_price_paise : 0
              const pct = i ? (ch * 100) / i.reference_price_paise : 0
              return (
                <tr
                  key={t}
                  aria-selected={t === selected}
                  onClick={() => setSelected(t)}
                  title={`Key ${n + 1}`}
                >
                  <td className="tk">{t}</td>
                  <td className="nm">{i?.name ?? ''}</td>
                  <td className="num">
                    <Flash value={px}>{px === null ? '—' : rupees(px)}</Flash>
                  </td>
                  <td className={`num ${direction(ch)}`}>
                    {arrow(ch)} {signedRupees(ch)}
                  </td>
                  <td className={`num ${direction(ch)}`}>{percent(pct)}</td>
                  <td className="num">
                    {m.book?.best_bid_paise ? rupees(m.book.best_bid_paise) : '—'}
                  </td>
                  <td className="num">
                    {m.book?.best_ask_paise ? rupees(m.book.best_ask_paise) : '—'}
                  </td>
                  <td className="num">{i ? rupees(i.day_high_paise) : '—'}</td>
                  <td className="num">{i ? rupees(i.day_low_paise) : '—'}</td>
                  <td className="num">{i ? quantity(i.day_volume) : '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </section>

      <div className="dash-main">
        <section className="panel hero-panel" aria-label={`${selected} chart`}>
          <h2>
            <span>
              <b className="terminal-only">{selected}</b>
              <span className="paper-only">{selected}</span> {info?.name ?? ''}
            </span>
            <small>5-second candles</small>
          </h2>
          <div className="hero">
            <div className="price" data-testid="hero-price">
              <span className="rs paper-only">₹</span>
              <Flash value={last}>{last === null ? '—' : rupees(last)}</Flash>
            </div>
            <div className={`change ${direction(change)}`}>
              {arrow(change)} {signedRupees(change)}
              <small>
                {info ? percent((change * 100) / info.reference_price_paise) : ''} today
              </small>
            </div>
            <JoinBox />
          </div>
          <div className="chart-box">
            <CandleChart candles={view.candles['5s']} />
          </div>
          <div className="chart-caption paper-only">
            <span className="label">Price, ₹ · last ten minutes</span>
            <span className="caption">Each bar is five seconds of trading</span>
          </div>
        </section>

        <div className="dash-side">
          <section className="panel depth" aria-label="Order book">
            <h2>
              <span>
                Order book <b className="terminal-only">{selected}</b>
              </span>
              <small>Best {DEPTH_ROWS} levels</small>
            </h2>
            <table>
              <thead>
                <tr>
                  <th>Qty</th>
                  <th>Bid ₹</th>
                  <th>Ask ₹</th>
                  <th>Qty</th>
                </tr>
              </thead>
              <tbody>
                {Array.from({ length: DEPTH_ROWS }, (_, n) => {
                  const b = book?.bids[n]
                  const a = book?.asks[n]
                  return (
                    <tr key={n}>
                      <td className="num bar bid-bar">
                        {b && <i style={{ width: `${(b.quantity / maxQty) * 100}%` }} />}
                        <span>{b ? quantity(b.quantity) : ''}</span>
                      </td>
                      <td className="num up">{b ? rupees(b.price_paise) : ''}</td>
                      <td className="num down">{a ? rupees(a.price_paise) : ''}</td>
                      <td className="num bar ask-bar">
                        {a && <i style={{ width: `${(a.quantity / maxQty) * 100}%` }} />}
                        <span>{a ? quantity(a.quantity) : ''}</span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            <div className="spread caption">
              {book?.spread_paise != null
                ? `Spread ₹${rupees(book.spread_paise)}`
                : 'No two-sided quote yet'}
            </div>
          </section>

          <section className="panel tape" aria-label="Trades">
            <h2>
              <span>Trades</span>
              <small>Newest first</small>
            </h2>
            <table>
              <tbody>
                {view.trades.slice(0, TAPE_ROWS).map((t, n) => (
                  <tr key={t.trade_id} className={n === 0 ? 'enter' : ''}>
                    <td className="num">{clock(t.executed_at)}</td>
                    <td className={`side ${t.aggressor_side === 'BUY' ? 'up' : 'down'}`}>
                      {t.aggressor_side === 'BUY' ? '▲ BUY' : '▼ SELL'}
                    </td>
                    <td className="num">{quantity(t.quantity)}</td>
                    <td className="num">{rupees(t.price_paise)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {view.trades.length === 0 && <p className="empty">No trades yet.</p>}
          </section>
        </div>
      </div>

      <footer className="statusbar">
        <span className={health === 'all systems healthy' ? 'up' : 'down'}>● {health}</span>
        <span>
          Streams <b>{live}/5</b> {state !== 'live' && '· reconnecting'}
        </span>
        <span>
          Volume today <b>{quantity(volume)}</b>
        </span>
        <span className="end">
          {selected} sequence <b>{quantity(view.seenTrade)}</b>
        </span>
        <span className="attribution">
          Charts by{' '}
          <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">
            TradingView
          </a>
        </span>
      </footer>
    </div>
  )
}

/** J-01: the QR code that phones scan to join (it encodes PUBLIC_BASE_URL/join). */
function JoinBox() {
  return (
    <aside className="join" aria-label="Join">
      <QrCode text={joinUrl()} size={196} />
      <div>
        <div className="head">
          <span className="terminal-only">
            <b>F8</b>{' '}
          </span>
          Scan to trade.
          <br />
          <span className="paper-only">₹1,00,000 to start.</span>
        </div>
        <p>
          A nickname, ₹1,00,000 of play money.
          <br />
          No password.
        </p>
        <div className="url">{joinUrl()}</div>
      </div>
    </aside>
  )
}
