// The presenter's projector view (web/README.md "Presenter"; admin only): every ticker live, the
// selected one big, its depth and trades, the leaderboard (the presenter can hide it), and the QR
// code to join. One layout for both skins; the masthead and dateline (paper) and the command line
// and F-keys (terminal) are theme-only details.
import { useEffect, useRef, useState } from 'react'
import { CandleChart } from '../components/CandleChart'
import { Flash } from '../components/Flash'
import { Leaderboard } from '../components/Leaderboard'
import { FKeys, PresenterGate, Sections } from '../components/PresenterNav'
import { QrCode } from '../components/QrCode'
import { ThemeToggle } from '../components/ThemeToggle'
import { joinUrl, joinUrlWarning } from '../lib/config'
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
import { loadLeaderboardShown, saveLeaderboardShown } from '../lib/session'
import { useInstruments, useLeaderboard, useMarkets } from '../market/hooks'
import { useHealth, useNow, useRowsThatFit } from './presenterHooks'

const DEPTH_ROWS = 6
const NBSP = '\u00a0' // an empty level keeps its row's height, so the panels below never jump
const TAPE_ROWS = 12 // at most; the tape shows the whole rows that fit (useRowsThatFit)

export function Dashboard() {
  return (
    <PresenterGate purpose="The projector dashboard is for the presenter. Sign in with the presenter passcode.">
      {(token, onExpired) => <Board token={token} onExpired={onExpired} />}
    </PresenterGate>
  )
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
  const tape = useRef<HTMLElement>(null)
  // Projector safety: the presenter can take the leaderboard off the screen (remembered here).
  const [boardShown, setBoardShown] = useState(loadLeaderboardShown)
  const toggleBoard = () =>
    setBoardShown((shown) => {
      saveLeaderboardShown(!shown)
      return !shown
    })
  const board = useLeaderboard(token, boardShown)

  // Keys 1 to 5 pick the ticker shown big (the presenter's remote clicker can send them); B shows
  // or hides the leaderboard.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const n = Number(e.key)
      if (n >= 1 && n <= TICKERS.length) setSelected(TICKERS[n - 1])
      else if (e.key === 'b' || e.key === 'B') toggleBoard()
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
  const tapeRows = useRowsThatFit(tape, TAPE_ROWS, view.trades.length > 0)
  const maxQty = Math.max(
    1,
    ...(book ? [...book.bids, ...book.asks].slice(0, 99).map((l) => l.quantity) : [1]),
  )

  return (
    <div className="dash">
      <header className="dash-head">
        <div className="title paper-only">
          <h1 className="masthead">
            CircuitBreaker <em>Markets</em>
          </h1>
          <Sections active="market" />
        </div>
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
          <button type="button" className="theme-toggle" onClick={toggleBoard} title="Key B">
            {boardShown ? 'Hide leaderboard' : 'Show leaderboard'}
          </button>
          <ThemeToggle />
        </div>
      </header>
      <FKeys active="market" />

      <div className={`dash-top${boardShown ? ' with-board' : ''}`}>
        <section className={`panel watch${boardShown ? ' compact' : ''}`} aria-label="Watchlist">
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
                <th className="nm">Name</th>
                <th>Last ₹</th>
                <th>Change</th>
                <th>Change %</th>
                <th>Bid</th>
                <th>Ask</th>
                <th className="hl">High</th>
                <th className="hl">Low</th>
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
                    <td className="num hl">{i ? rupees(i.day_high_paise) : '—'}</td>
                    <td className="num hl">{i ? rupees(i.day_low_paise) : '—'}</td>
                    <td className="num">{i ? quantity(i.day_volume) : '—'}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </section>
        {boardShown && <Leaderboard board={board} />}
      </div>

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
                        <span>{b ? quantity(b.quantity) : NBSP}</span>
                      </td>
                      <td className="num up">{b ? rupees(b.price_paise) : NBSP}</td>
                      <td className="num down">{a ? rupees(a.price_paise) : NBSP}</td>
                      <td className="num bar ask-bar">
                        {a && <i style={{ width: `${(a.quantity / maxQty) * 100}%` }} />}
                        <span>{a ? quantity(a.quantity) : NBSP}</span>
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

          <section className="panel tape" aria-label="Trades" ref={tape}>
            <h2>
              <span>Trades</span>
              <small>Newest first</small>
            </h2>
            <table>
              <tbody>
                {view.trades.slice(0, tapeRows).map((t, n) => (
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

/** The join address as people type it (no http://), breaking only before the path. */
function shownUrl(url: string) {
  const bare = url.replace(/^https?:\/\//, '')
  const slash = bare.indexOf('/')
  return slash < 0 ? (
    bare
  ) : (
    <>
      {bare.slice(0, slash)}
      <wbr />
      {bare.slice(slash)}
    </>
  )
}

/** J-01: the QR code that phones scan to join (it encodes PUBLIC_BASE_URL/join). If that address
 * only works on this laptop (localhost, or PUBLIC_BASE_URL unset), the presenter is told so. */
export function JoinBox() {
  const url = joinUrl()
  const warning = joinUrlWarning()
  return (
    <aside className="join" aria-label="Join">
      <QrCode text={url} size={196} />
      <div>
        <div className="head">
          <span className="terminal-only">
            <b>F8</b>{' '}
          </span>
          Scan to trade.
          <br />
          <span className="paper-only">₹1,00,000 to start.</span>
        </div>
        {warning ? (
          <p className="qr-warning" role="alert">
            {warning}
          </p>
        ) : (
          <p>
            A nickname, ₹1,00,000 of play money.
            <br />
            No password.
          </p>
        )}
        <div className="url">{shownUrl(url)}</div>
      </div>
    </aside>
  )
}
