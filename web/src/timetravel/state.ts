// The time-travel screen's state (presenter only; J-20 → Q-12, Q-13), as one reducer so every
// change is testable. `live` shows the live market; every other mode shows the past, and the
// screen must say so on every panel (historical()).
//
// - seek: Q-12 rebuilds the market at T (book, trades, last price, hash); the chart's 5-second
//   candles come from Q-03 up to T, the one holding T recomputed from the trades up to T.
// - play: Q-13 replays from T at a speed; each frame replaces the book, adds its trade to the tape
//   and its forming candle to the chart. Frames older than what is shown are dropped.
// - back to live: the live market again, and a rewind to now proves the rebuild exact (Q-12's
//   `verification`: the rebuilt hash against the live engine's, at the same sequence).
import type {
  Candle,
  OrderBook,
  ReplayEnd,
  ReplayFrame,
  TimeTravelView,
  Trade,
  Verification,
} from '../lib/market'

export type Mode = 'live' | 'seeking' | 'paused' | 'playing' | 'ended'
export const SPEEDS = [1, 2, 5, 10, 60] as const // api/README.md, Q-13
export type Speed = (typeof SPEEDS)[number]
export const MAX_REPLAY_MS = 30 * 60_000 // a replay window is at most 30 minutes
const CANDLE_MS = 5_000
const KEEP_CANDLES = 240 // 20 minutes of 5-second candles on the chart
const KEEP_TRADES = 50

export interface HistoryView {
  at: number // ms: the moment shown (the replay's position while playing)
  sequence: number
  book: OrderBook | null
  lastPrice: number | null
  candles: Candle[] // 5-second, oldest first
  trades: Trade[] // newest first
  stateHash: string | null
}

export interface ReplayCheck {
  end: ReplayEnd
  matches: boolean | null // the end hash against Q-12 at `to`; null until checked
}

export interface TimeTravelState {
  mode: Mode
  speed: Speed
  target: number | null // where the last seek is going (the banner shows it while loading)
  view: HistoryView | null
  verification: Verification | null // from the last rewind to now
  replay: ReplayCheck | null
  error: string | null
}

export type Action =
  | { kind: 'seek'; at: number }
  | { kind: 'loaded'; at: number; view: TimeTravelView; candles: Candle[] }
  | { kind: 'play' }
  | { kind: 'frame'; frame: ReplayFrame }
  | { kind: 'pause' }
  | { kind: 'resume' }
  | { kind: 'speed'; speed: Speed }
  | { kind: 'end'; end: ReplayEnd }
  | { kind: 'replay-checked'; matches: boolean }
  | { kind: 'live' }
  | { kind: 'verified'; verification: Verification | null }
  | { kind: 'error'; message: string }

export const initialState: TimeTravelState = {
  mode: 'live',
  speed: 10,
  target: null,
  view: null,
  verification: null,
  replay: null,
  error: null,
}

/** True in every mode that shows the past: the screen must never pass it off as live. */
export const historical = (mode: Mode) => mode !== 'live'

export const ms = (iso: string) => Date.parse(iso)
const bucketOf = (at: number) => at - (at % CANDLE_MS)

/** The 5-second candles as they were at `at`: none after it, and the one holding `at` rebuilt
 * from the trades up to `at` (flat at the last price when there were none). */
export function candlesUpTo(
  candles: Candle[],
  trades: Trade[],
  at: number,
  lastPrice: number | null,
): Candle[] {
  const start = bucketOf(at)
  const before = candles.filter((c) => ms(c.bucket_start) < start)
  const inBucket = trades
    .filter((t) => ms(t.executed_at) >= start && ms(t.executed_at) <= at)
    .sort((a, b) => a.sequence - b.sequence)
  const close = lastPrice ?? before[before.length - 1]?.close_paise
  if (close === undefined) return before
  const prices = inBucket.map((t) => t.price_paise)
  const forming: Candle = prices.length
    ? {
        bucket_start: new Date(start).toISOString(),
        open_paise: prices[0],
        high_paise: Math.max(...prices),
        low_paise: Math.min(...prices),
        close_paise: prices[prices.length - 1],
        volume: inBucket.reduce((sum, t) => sum + t.quantity, 0),
        trade_count: prices.length,
      }
    : {
        bucket_start: new Date(start).toISOString(),
        open_paise: close,
        high_paise: close,
        low_paise: close,
        close_paise: close,
        volume: 0,
        trade_count: 0,
      }
  return [...before, forming]
}

function withCandle(candles: Candle[], candle: Candle): Candle[] {
  const last = candles[candles.length - 1]
  if (last && last.bucket_start === candle.bucket_start) return [...candles.slice(0, -1), candle]
  if (last && ms(candle.bucket_start) < ms(last.bucket_start)) return candles
  return [...candles, candle].slice(-KEEP_CANDLES)
}

export function applyFrame(view: HistoryView, frame: ReplayFrame): HistoryView {
  if (frame.as_of_sequence < view.sequence) return view // older than what is shown
  const trade = frame.last_trade
  const fresh = trade && !view.trades.some((t) => t.sequence >= trade.sequence)
  return {
    at: ms(frame.as_of),
    sequence: frame.as_of_sequence,
    book: frame.order_book,
    lastPrice: trade ? trade.price_paise : view.lastPrice,
    candles: withCandle(view.candles, frame.candle),
    trades: fresh ? [trade, ...view.trades].slice(0, KEEP_TRADES) : view.trades,
    stateHash: null, // known again at a seek or at the replay's end
  }
}

export function reduce(state: TimeTravelState, action: Action): TimeTravelState {
  switch (action.kind) {
    case 'seek':
      return { ...state, mode: 'seeking', target: action.at, replay: null, error: null }
    case 'loaded': {
      if (action.at !== state.target) return state // a newer seek is on its way
      const v = action.view
      const lastPrice = v.last_price_paise
      return {
        ...state,
        mode: 'paused',
        view: {
          at: action.at,
          sequence: v.as_of_sequence,
          book: v.order_book,
          lastPrice,
          candles: candlesUpTo(action.candles, v.recent_trades, action.at, lastPrice),
          trades: v.recent_trades,
          stateHash: v.state_hash,
        },
        verification: v.verification ?? state.verification,
      }
    }
    case 'play':
      return state.view ? { ...state, mode: 'playing', replay: null, error: null } : state
    case 'frame':
      return state.view && (state.mode === 'playing' || state.mode === 'paused')
        ? { ...state, view: applyFrame(state.view, action.frame) }
        : state
    case 'pause':
      return state.mode === 'playing' ? { ...state, mode: 'paused' } : state
    case 'resume':
      return state.mode === 'paused' && state.view ? { ...state, mode: 'playing' } : state
    case 'speed':
      return { ...state, speed: action.speed }
    case 'end':
      return state.view
        ? {
            ...state,
            mode: 'ended',
            view: { ...state.view, at: ms(action.end.to), stateHash: action.end.state_hash },
            replay: { end: action.end, matches: null },
          }
        : state
    case 'replay-checked':
      return state.replay
        ? { ...state, replay: { ...state.replay, matches: action.matches } }
        : state
    case 'live':
      return { ...state, mode: 'live', target: null, view: null, replay: null, error: null }
    case 'verified':
      return { ...state, verification: action.verification }
    case 'error':
      return { ...state, error: action.message }
  }
}

export interface Moment {
  at: number // the minute's start, ms
  change: number // close − open, paise
}

/** Notable moments for the timeline: the `count` biggest one-minute moves, in time order. */
export function notableMoments(candles: Candle[], count = 5): Moment[] {
  return candles
    .map((c) => ({ at: ms(c.bucket_start), change: c.close_paise - c.open_paise }))
    .filter((m) => m.change !== 0)
    .sort((a, b) => Math.abs(b.change) - Math.abs(a.change) || a.at - b.at)
    .slice(0, count)
    .sort((a, b) => a.at - b.at)
}

/** Where a replay from `from` stops: 30 minutes later at most, and never after now. */
export function replayEnd(from: number, now: number): number {
  return Math.min(from + MAX_REPLAY_MS, now - 1_000)
}
