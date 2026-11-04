// One ticker's live market from Q-06 (api/README.md): a snapshot, then updates in sequence.
// The rules (the same as services/market-data-service/tests/unit/test_ws.py):
//   - a snapshot replaces everything (on every connect and reconnect);
//   - a trade at or below the sequence already known is dropped (it is in the snapshot or seen);
//   - a book replaces the book only if it is as new or newer;
//   - a candle replaces the candle with the same bucket_start for its interval.
// So a client that lost its connection and took a fresh snapshot ends exactly where one that never
// left is: no gaps, no duplicates.
import type { Candle, OrderBook, Trade } from '../lib/market'

export type Interval = '5s' | '1m'
export const MAX_TRADES = 50
export const MAX_CANDLES = 120

export interface MarketView {
  ready: boolean
  book: OrderBook | null
  lastPrice: number | null
  trades: Trade[] // newest first
  seenTrade: number // highest trade sequence applied
  candles: Record<Interval, Candle[]> // oldest first
}

export const emptyMarket: MarketView = {
  ready: false,
  book: null,
  lastPrice: null,
  trades: [],
  seenTrade: 0,
  candles: { '5s': [], '1m': [] },
}

export type MarketMessage =
  | {
      type: 'snapshot'
      data: {
        order_book: OrderBook
        last_price_paise: number
        candles: Candle[]
        recent_trades?: Trade[]
      }
    }
  | { type: 'trade'; data: Trade }
  | { type: 'book'; data: OrderBook }
  | { type: 'candle'; data: { interval: string; closed: boolean; candle: Candle } }
  | { type: 'ping' }

function upsert(list: Candle[], candle: Candle): Candle[] {
  const last = list[list.length - 1]
  if (last && last.bucket_start === candle.bucket_start) return [...list.slice(0, -1), candle]
  if (!last || candle.bucket_start > last.bucket_start) return [...list, candle].slice(-MAX_CANDLES)
  return list.map((c) => (c.bucket_start === candle.bucket_start ? candle : c)) // a correction
}

export function applyMarket(state: MarketView, message: MarketMessage): MarketView {
  switch (message.type) {
    case 'snapshot': {
      const { order_book, last_price_paise, candles, recent_trades = [] } = message.data
      return {
        ready: true,
        book: order_book,
        lastPrice: last_price_paise,
        trades: recent_trades.slice(0, MAX_TRADES),
        seenTrade: Math.max(order_book.as_of_sequence, ...recent_trades.map((t) => t.sequence), 0),
        candles: { '5s': candles.slice(-MAX_CANDLES), '1m': state.candles['1m'] },
      }
    }
    case 'trade': {
      const t = message.data
      if (!state.ready || t.sequence <= state.seenTrade) return state
      return {
        ...state,
        lastPrice: t.price_paise,
        trades: [t, ...state.trades].slice(0, MAX_TRADES),
        seenTrade: t.sequence,
      }
    }
    case 'book': {
      if (state.book && message.data.as_of_sequence < state.book.as_of_sequence) return state
      return { ...state, book: message.data }
    }
    case 'candle': {
      const { interval, candle } = message.data
      if (interval !== '5s' && interval !== '1m') return state
      return {
        ...state,
        candles: { ...state.candles, [interval]: upsert(state.candles[interval], candle) },
      }
    }
    default:
      return state
  }
}

/** Seed a candle interval from Q-03 (the snapshot carries only 5s candles). */
export function withHistory(state: MarketView, interval: Interval, candles: Candle[]): MarketView {
  let merged = state.candles[interval]
  for (const c of candles) merged = upsert(merged, c)
  return { ...state, candles: { ...state.candles, [interval]: merged } }
}
