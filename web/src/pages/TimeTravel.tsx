// Time travel, the presenter's event-sourcing demo (J-20 → Q-12, Q-13; admin only). The same
// layout as the dashboard, with a timeline instead of the watchlist: scrub to any moment of the
// session and the market is rebuilt as it was; play from there at 1× to 60×; "Back to live"
// returns to the live market and proves the rebuild exact (the rebuilt hash equals the live
// engine's at the same sequence). In the past, a banner and every panel say so.
//
// Keys: 1–5 choose the ticker; Space plays or pauses; L goes back to live. The timeline's handle
// has its own keys (components/Timeline.tsx).
import { useEffect, useMemo, useRef, useState } from 'react'
import { CandleChart } from '../components/CandleChart'
import { Flash } from '../components/Flash'
import { HistoricalBanner, HistoryTag } from '../components/Historical'
import { FKeys, PresenterGate, Sections } from '../components/PresenterNav'
import { ThemeToggle } from '../components/ThemeToggle'
import { Timeline } from '../components/Timeline'
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
import {
  api,
  type Candle,
  type Instrument,
  type Ticker,
  TICKERS,
  type Verification,
} from '../lib/market'
import { useInstruments, useMarket } from '../market/hooks'
import { historical, notableMoments, SPEEDS, type TimeTravelState } from '../timetravel/state'
import { useTimeTravel } from '../timetravel/useTimeTravel'
import { useHealth, useNow, useRowsThatFit } from './presenterHooks'

const DEPTH_ROWS = 6
const NBSP = '\u00a0' // an empty level keeps its row's height, so the panels below never jump
const TAPE_ROWS = 12
const SESSION_MS = 500 * 60_000 // the timeline: the last 500 one-minute candles (Q-03's limit)

export function TimeTravel() {
  return (
    <PresenterGate purpose="Time travel is for the presenter. Sign in with the presenter passcode.">
      {(token, onExpired) => <Screen token={token} onExpired={onExpired} />}
    </PresenterGate>
  )
}

/** The session's one-minute candles for the timeline, refreshed as the session grows. */
function useSession(token: string, ticker: Ticker, everyMs = 30_000): Candle[] {
  const [candles, setCandles] = useState<Candle[]>([])
  useEffect(() => {
    let stop = false
    const load = () => {
      const to = new Date()
      const from = new Date(to.getTime() - SESSION_MS)
      api
        .candlesBetween(token, ticker, '1m', from.toISOString(), to.toISOString(), 500)
        .then((c) => !stop && setCandles(c))
        .catch(() => {})
    }
    load()
    const id = window.setInterval(load, everyMs)
    return () => {
      stop = true
      window.clearInterval(id)
    }
  }, [token, ticker, everyMs])
  return candles
}

/** The hero's state-hash fact, always saying which sequence a hash belongs to. */
function hashFact(state: TimeTravelState, stateHash: string | null): string {
  const short = (hash: string) => `${hash.slice(0, 12)}…`
  switch (state.mode) {
    case 'live': {
      const v = state.verification
      return v?.matches
        ? `verified at ${quantity(v.live_sequence)} · ${short(v.live_state_hash)}`
        : 'not verified yet'
    }
    case 'seeking':
      return 'rebuilding…'
    case 'playing':
      return 'computed when paused'
    default:
      return stateHash ? short(stateHash) : 'computing…'
  }
}

function statusText(state: TimeTravelState): string {
  const v = state.view
  const seq = v ? `sequence ${quantity(v.sequence)}` : ''
  switch (state.mode) {
    case 'seeking':
      return 'Rebuilding the market from the event store…'
    case 'paused':
      return `Paused · ${seq}`
    case 'playing':
      return `Replaying at ${state.speed}× · ${seq}`
    case 'ended':
      return state.replay?.matches === true
        ? `Replay ended · ${seq} · equals time travel to the same moment`
        : state.replay?.matches === false
          ? `Replay ended · ${seq} · DIFFERS from time travel to the same moment`
          : `Replay ended · ${seq}`
    default:
      return ''
  }
}

/** The proof: the rebuilt market against the live engine's book, at the same sequence (which
 * it names, so it is never confused with the sequence shown elsewhere). */
function Proof({ verification }: { verification: Verification | null }) {
  if (!verification) return null
  return verification.matches ? (
    <span className="proof up" data-testid="proof">
      ● Verified at sequence {quantity(verification.live_sequence)}: hashes match{' '}
      <span className="dim">· {verification.live_state_hash.slice(0, 12)}…</span>
    </span>
  ) : (
    <span className="proof down" data-testid="proof">
      ● Hashes differ at sequence {quantity(verification.live_sequence)}
    </span>
  )
}

function Screen({ token, onExpired }: { token: string; onExpired: () => void }) {
  const [ticker, setTicker] = useState<Ticker>('CALVERA')
  const [live, liveState] = useMarket(ticker, token)
  const instruments = useInstruments(token)
  const session = useSession(token, ticker)
  const tt = useTimeTravel(token, ticker)
  const { state } = tt
  const now = useNow()
  const health = useHealth(token, onExpired)
  const tape = useRef<HTMLElement>(null)
  const past = historical(state.mode)
  const view = state.view

  // What the panels show: the rebuilt past, or the live market.
  const shown = past
    ? {
        at: view?.at ?? state.target ?? now.getTime(),
        lastPrice: view?.lastPrice ?? null,
        candles: view?.candles ?? [],
        book: view?.book ?? null,
        trades: view?.trades ?? [],
        sequence: view?.sequence ?? 0,
        stateHash: view?.stateHash ?? null,
      }
    : {
        at: now.getTime(),
        lastPrice: live.lastPrice,
        candles: live.candles['5s'],
        book: live.book,
        trades: live.trades,
        sequence: live.book?.as_of_sequence ?? live.seenTrade, // the book's last event
        stateHash: null,
      }
  const info: Instrument | undefined = instruments.find((i) => i.ticker === ticker)
  const last = shown.lastPrice ?? (past ? null : (info?.last_price_paise ?? null))
  const change = last !== null && info ? last - info.reference_price_paise : 0
  // The session starts at its first trade (flat minutes before it are only the service idling).
  const traded = useMemo(() => {
    const first = session.findIndex((c) => c.trade_count > 0)
    return first < 0 ? session.slice(-60) : session.slice(Math.max(0, first - 1))
  }, [session])
  const moments = useMemo(() => notableMoments(traded), [traded])
  const start = traded.length ? Date.parse(traded[0].bucket_start) : now.getTime() - 3_600_000
  const tapeRows = useRowsThatFit(tape, TAPE_ROWS, shown.trades.length > 0)
  const book = shown.book
  const maxQty = Math.max(1, ...(book ? [...book.bids, ...book.asks].map((l) => l.quantity) : [1]))
  const tag = past ? <HistoryTag at={shown.at} /> : null

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target instanceof Element ? e.target : null
      if (target?.closest('input, [role="slider"], button')) return // their own keys
      const n = Number(e.key)
      if (n >= 1 && n <= TICKERS.length) setTicker(TICKERS[n - 1])
      else if (e.key === ' ' && past) {
        e.preventDefault()
        if (state.mode === 'playing') tt.pause()
        else tt.play()
      } else if (e.key === 'l' || e.key === 'L') tt.backToLive()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [past, state.mode, tt])

  return (
    <div className={`dash time-travel${past ? ' is-historical' : ''}`}>
      <header className="dash-head">
        <div className="title paper-only">
          <h1 className="masthead">
            CircuitBreaker <em>Markets</em>
          </h1>
          <Sections active="time-travel" />
        </div>
        <span className="dateline paper-only">
          {past ? 'The archive: the market rebuilt from its events' : `${dateline(now)} · live`}
        </span>
        <div className="command terminal-only" aria-label="Command line">
          <span className="badge">CIRCUITBREAKER</span>
          <span>
            &gt; <span className="typed">{ticker}</span> EQUITY TIME TRAVEL
            <span className="cursor" aria-hidden="true" />
          </span>
        </div>
        <div className="right">
          <span>
            {past ? 'Archive' : 'Live'} · <b>{clock(new Date(shown.at))}</b> IST
          </span>
          <ThemeToggle />
        </div>
      </header>
      <FKeys active="time-travel" />

      {past ? (
        <HistoricalBanner at={shown.at} status={statusText(state)} onBackToLive={tt.backToLive} />
      ) : (
        <div className="live-strip" role="status" aria-label="Live">
          <span className="up">● Live market</span>
          <span className="dim">Drag the timeline back to rebuild any moment of the session.</span>
          <Proof verification={state.verification} />
        </div>
      )}

      <section className="panel tt-timeline" aria-label="Timeline">
        <h2>
          <span className="tickers" role="group" aria-label="Ticker">
            {TICKERS.map((t, n) => (
              <button
                key={t}
                type="button"
                aria-pressed={t === ticker}
                onClick={() => setTicker(t)}
                title={`Key ${n + 1}`}
              >
                {t}
              </button>
            ))}
          </span>
          {tag}
          <small>Biggest one-minute moves marked ▲ ▼</small>
        </h2>
        <Timeline
          candles={traded}
          start={start}
          end={now.getTime()}
          position={past ? shown.at : null}
          moments={moments}
          onSeek={tt.seek}
          onSeekNow={tt.seekNow}
        />
        <div className="controls">
          <button
            type="button"
            className="play"
            disabled={!past || state.mode === 'seeking'}
            onClick={state.mode === 'playing' ? tt.pause : tt.play}
          >
            {state.mode === 'playing' ? '❚❚ Pause' : '▶ Play'}
          </button>
          <span className="speeds" role="group" aria-label="Replay speed">
            {SPEEDS.map((s) => (
              <button
                key={s}
                type="button"
                aria-pressed={state.speed === s}
                onClick={() => tt.setSpeed(s)}
              >
                {s}×
              </button>
            ))}
          </span>
          <button type="button" onClick={tt.verify} disabled={state.mode === 'seeking'}>
            Verify against live
          </button>
          {past && <Proof verification={state.verification} />}
          {state.error && (
            <span className="problem" role="alert">
              {state.error}
            </span>
          )}
        </div>
      </section>

      <div className="dash-main">
        <section className="panel hero-panel" aria-label={`${ticker} chart`}>
          <h2>
            <span>
              <b className="terminal-only">{ticker}</b>
              <span className="paper-only">{ticker}</span> {info?.name ?? ''}
            </span>
            {tag}
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
                {info ? percent((change * 100) / info.reference_price_paise) : ''} on the day
              </small>
            </div>
            <dl className="facts">
              <dt>Sequence</dt>
              <dd className="num">{state.mode === 'seeking' ? '…' : quantity(shown.sequence)}</dd>
              <dt>State hash</dt>
              <dd className="num" data-testid="state-hash">
                {hashFact(state, shown.stateHash)}
              </dd>
            </dl>
          </div>
          <div className="chart-box">
            <CandleChart candles={shown.candles} />
          </div>
          <div className="chart-caption paper-only">
            <span className="label">Price, ₹</span>
            <span className="caption">
              {past
                ? 'As it stood then; each bar is five seconds'
                : 'Each bar is five seconds of trading'}
            </span>
          </div>
        </section>

        <div className="dash-side">
          <section className="panel depth" aria-label="Order book">
            <h2>
              <span>
                Order book <b className="terminal-only">{ticker}</b>
              </span>
              {tag}
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
                : 'No two-sided quote'}
            </div>
          </section>

          <section className="panel tape" aria-label="Trades" ref={tape}>
            <h2>
              <span>Trades</span>
              {tag}
              <small>Newest first</small>
            </h2>
            <table>
              <tbody>
                {shown.trades.slice(0, tapeRows).map((t) => (
                  <tr key={t.trade_id}>
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
            {shown.trades.length === 0 && <p className="empty">No trades.</p>}
          </section>
        </div>
      </div>

      <footer className="statusbar">
        <span className={health === 'all systems healthy' ? 'up' : 'down'}>● {health}</span>
        <span>
          {past
            ? 'Rebuilt from the event store'
            : `Stream ${liveState === 'live' ? 'live' : 'reconnecting'}`}
        </span>
        <span className="end">
          {ticker} sequence <b>{quantity(shown.sequence)}</b>
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
