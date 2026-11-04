// The web client's half of the resync guarantee (the server's half: market-data test_ws.py).
import type { Candle, OrderBook, Trade } from '../lib/market'
import {
  applyMarket,
  emptyMarket,
  type MarketMessage,
  type MarketView,
  MAX_TRADES,
} from './marketState'

let seq = 100
const trades: Trade[] = []
const book = (s: number, bid: number): OrderBook => ({
  ticker: 'CHAIWALA',
  as_of_sequence: s,
  as_of: '2026-11-20T10:42:00.000Z',
  best_bid_paise: bid,
  best_ask_paise: bid + 10,
  spread_paise: 10,
  bids: [{ price_paise: bid, quantity: 5, order_count: 1 }],
  asks: [{ price_paise: bid + 10, quantity: 5, order_count: 1 }],
})
const candle = (n: number, close: number): Candle => ({
  bucket_start: `2026-11-20T10:42:${String(n * 5).padStart(2, '0')}Z`,
  open_paise: close,
  high_paise: close,
  low_paise: close,
  close_paise: close,
  volume: 1,
  trade_count: 1,
})

/** The server: every message it would send, and the snapshot it would build at any moment. */
function step(n: number): MarketMessage[] {
  seq += 2
  const t: Trade = {
    trade_id: `t${seq}`,
    sequence: seq,
    price_paise: 24_500 + n * 5,
    quantity: 1,
    aggressor_side: 'BUY',
    executed_at: '2026-11-20T10:42:00.000Z',
  }
  trades.unshift(t)
  return [
    { type: 'trade', data: t },
    { type: 'book', data: book(seq, 24_495 + n * 5) },
    {
      type: 'candle',
      data: { interval: '5s', closed: false, candle: candle(n % 12, t.price_paise) },
    },
  ]
}
function snapshot(candles: Candle[]): MarketMessage {
  return {
    type: 'snapshot',
    data: {
      order_book: book(seq, 24_495 + (seq % 50)),
      last_price_paise: trades[0]?.price_paise ?? 24_550,
      candles,
      recent_trades: trades.slice(0, MAX_TRADES),
    },
  }
}

describe('snapshot, then updates in sequence', () => {
  it('a client that reconnects ends with the same state as one that never left', () => {
    const candles: Candle[] = []
    let steady: MarketView = applyMarket(emptyMarket, snapshot(candles))
    let flaky: MarketView = steady
    for (let n = 0; n < 120; n++) {
      const messages = step(n)
      const c = (messages[2] as Extract<MarketMessage, { type: 'candle' }>).data.candle
      const i = candles.findIndex((x) => x.bucket_start === c.bucket_start)
      if (i >= 0) candles[i] = c
      else candles.push(c)
      for (const m of messages) steady = applyMarket(steady, m)
      const offline = n >= 40 && n < 90
      if (!offline) for (const m of messages) flaky = applyMarket(flaky, m)
      if (n === 90) {
        flaky = applyMarket(
          flaky,
          snapshot([...candles].sort((a, b) => a.bucket_start.localeCompare(b.bucket_start))),
        )
        for (const m of messages) flaky = applyMarket(flaky, m) // the update that followed the snapshot
      }
    }
    expect(flaky.trades.map((t) => t.sequence)).toEqual(steady.trades.map((t) => t.sequence))
    expect(new Set(flaky.trades.map((t) => t.sequence)).size).toBe(flaky.trades.length) // no duplicate
    expect(flaky.lastPrice).toBe(steady.lastPrice)
    expect(flaky.book?.as_of_sequence).toBe(steady.book?.as_of_sequence)
  })

  it('drops a trade already in the snapshot and an older book', () => {
    let s = applyMarket(emptyMarket, snapshot([]))
    const known = s.seenTrade
    s = applyMarket(s, { type: 'trade', data: { ...trades[0], sequence: known } })
    expect(s.trades.filter((t) => t.sequence === known).length).toBeLessThanOrEqual(1)
    const before = s.book
    s = applyMarket(s, { type: 'book', data: book((before?.as_of_sequence ?? 0) - 1, 1) })
    expect(s.book).toBe(before)
  })

  it('replaces the candle with the same bucket and keeps them in order', () => {
    let s = applyMarket(emptyMarket, snapshot([candle(0, 100), candle(1, 105)]))
    s = applyMarket(s, {
      type: 'candle',
      data: { interval: '5s', closed: true, candle: candle(1, 110) },
    })
    s = applyMarket(s, {
      type: 'candle',
      data: { interval: '5s', closed: false, candle: candle(2, 115) },
    })
    expect(s.candles['5s'].map((c) => c.close_paise)).toEqual([100, 110, 115])
  })

  it('ignores updates before the first snapshot', () => {
    expect(applyMarket(emptyMarket, step(0)[0]).trades).toEqual([])
  })
})
