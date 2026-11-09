// The time-travel reducer and its helpers: every mode change, frames in order, the past as it was.
import type { Candle, ReplayEnd, ReplayFrame, TimeTravelView, Trade } from '../lib/market'
import {
  applyFrame,
  candlesUpTo,
  historical,
  initialState,
  notableMoments,
  reduce,
  replayEnd,
  type TimeTravelState,
} from './state'

const T = Date.parse('2026-11-20T10:42:13.000Z')
const iso = (at: number) => new Date(at).toISOString()

function candle(at: number, close: number, count = 1): Candle {
  return {
    bucket_start: iso(at),
    open_paise: close,
    high_paise: close,
    low_paise: close,
    close_paise: close,
    volume: count * 10,
    trade_count: count,
  }
}
function trade(sequence: number, at: number, price: number): Trade {
  return {
    trade_id: `t${sequence}`,
    sequence,
    price_paise: price,
    quantity: 5,
    aggressor_side: 'BUY',
    executed_at: iso(at),
  }
}
const book = (sequence: number) => ({
  ticker: 'CHAIWALA' as const,
  as_of_sequence: sequence,
  as_of: iso(T),
  best_bid_paise: 24500,
  best_ask_paise: 24510,
  spread_paise: 10,
  bids: [{ price_paise: 24500, quantity: 5, order_count: 1 }],
  asks: [{ price_paise: 24510, quantity: 5, order_count: 1 }],
})
const view = (verification: TimeTravelView['verification'] = null): TimeTravelView => ({
  ticker: 'CHAIWALA',
  at: iso(T),
  as_of_sequence: 40,
  last_price_paise: 24505,
  order_book: book(40),
  candles: [],
  recent_trades: [trade(39, T - 2_000, 24505), trade(38, T - 4_000, 24495)],
  state_hash: 'a'.repeat(64),
  verification,
})
const frame = (sequence: number, at: number, price: number | null): ReplayFrame => ({
  as_of: iso(at),
  as_of_sequence: sequence,
  order_book: book(sequence),
  last_trade: price === null ? null : trade(sequence, at, price),
  candle: candle(at - (at % 5_000), price ?? 24505),
})
const end: ReplayEnd = {
  frames_sent: 3,
  from: iso(T),
  to: iso(T + 60_000),
  as_of_sequence: 44,
  state_hash: 'b'.repeat(64),
}

function paused(): TimeTravelState {
  const seeking = reduce(initialState, { kind: 'seek', at: T })
  return reduce(seeking, { kind: 'loaded', at: T, view: view(), candles: [] })
}

describe('the time-travel state', () => {
  it('goes live → seeking → paused → playing → ended → live, and only live is not the past', () => {
    let s = initialState
    expect(s.mode).toBe('live')
    s = reduce(s, { kind: 'seek', at: T })
    expect([s.mode, s.target]).toEqual(['seeking', T])
    s = reduce(s, { kind: 'loaded', at: T, view: view(), candles: [] })
    expect(s.mode).toBe('paused')
    expect(s.view?.stateHash).toBe('a'.repeat(64))
    s = reduce(s, { kind: 'play' })
    expect(s.mode).toBe('playing')
    s = reduce(s, { kind: 'pause' })
    expect(s.mode).toBe('paused')
    s = reduce(s, { kind: 'resume' })
    expect(s.mode).toBe('playing')
    s = reduce(s, { kind: 'end', end })
    expect([s.mode, s.view?.stateHash, s.replay?.matches]).toEqual(['ended', 'b'.repeat(64), null])
    s = reduce(s, { kind: 'replay-checked', matches: true })
    expect(s.replay?.matches).toBe(true)
    s = reduce(s, { kind: 'live' })
    expect([s.mode, s.view, s.replay]).toEqual(['live', null, null])
    expect(['seeking', 'paused', 'playing', 'ended'].every((m) => historical(m as never))).toBe(
      true,
    )
    expect(historical('live')).toBe(false)
  })

  it('ignores the answer to an older seek when a newer one is on its way', () => {
    let s = reduce(initialState, { kind: 'seek', at: T })
    s = reduce(s, { kind: 'seek', at: T + 60_000 })
    s = reduce(s, { kind: 'loaded', at: T, view: view(), candles: [] })
    expect([s.mode, s.view]).toEqual(['seeking', null])
  })

  it('keeps the proof from a rewind to now, and a back to live replaces it', () => {
    const proof = { live_sequence: 40, live_state_hash: 'a'.repeat(64), matches: true }
    let s = reduce(reduce(initialState, { kind: 'seek', at: T }), {
      kind: 'loaded',
      at: T,
      view: view(proof),
      candles: [],
    })
    expect(s.verification).toEqual(proof)
    s = reduce(s, { kind: 'verified', verification: { ...proof, live_sequence: 41 } })
    expect(s.verification?.live_sequence).toBe(41)
  })

  it('applies frames in order, adds each trade once and drops a frame older than what is shown', () => {
    let s = reduce(paused(), { kind: 'play' })
    s = reduce(s, { kind: 'frame', frame: frame(42, T + 1_000, 24520) })
    s = reduce(s, { kind: 'frame', frame: frame(42, T + 1_000, 24520) }) // the same again
    s = reduce(s, { kind: 'frame', frame: frame(43, T + 7_000, null) }) // no trade in this one
    expect(s.view?.trades.map((t) => t.sequence)).toEqual([42, 39, 38])
    expect([s.view?.sequence, s.view?.lastPrice, s.view?.at]).toEqual([43, 24520, T + 7_000])
    expect(s.view?.stateHash).toBeNull() // known again at the end or the next seek
    const before = s.view
    s = reduce(s, { kind: 'frame', frame: frame(41, T + 500, 24400) })
    expect(s.view).toBe(before)
  })

  it('a frame replaces the forming candle and opens the next one', () => {
    const v = paused().view!
    const a = applyFrame(v, frame(41, T + 1_000, 24520))
    const b = applyFrame(a, frame(42, T + 1_500, 24530)) // same 5 s bucket (10:42:10)
    const c = applyFrame(b, frame(43, T + 6_000, 24540)) // the next one (10:42:15)
    expect(b.candles.length).toBe(a.candles.length)
    expect(b.candles.at(-1)?.close_paise).toBe(24530)
    expect(c.candles.length).toBe(b.candles.length + 1)
  })

  it('pausing, resuming and frames do nothing outside a replay', () => {
    expect(reduce(initialState, { kind: 'pause' })).toBe(initialState)
    expect(reduce(initialState, { kind: 'resume' })).toBe(initialState)
    expect(reduce(initialState, { kind: 'play' })).toBe(initialState)
    expect(reduce(initialState, { kind: 'frame', frame: frame(1, T, 1) })).toBe(initialState)
    expect(reduce(initialState, { kind: 'end', end })).toBe(initialState)
    expect(reduce(initialState, { kind: 'speed', speed: 60 }).speed).toBe(60)
    expect(reduce(initialState, { kind: 'error', message: 'busy' }).error).toBe('busy')
  })
})

describe('the past, exactly', () => {
  it('drops candles after T and rebuilds the one holding T from the trades up to T', () => {
    const bucket = T - (T % 5_000) // 10:42:10
    const stored = [
      candle(bucket - 5_000, 24490),
      candle(bucket, 24600, 4),
      candle(bucket + 5_000, 1),
    ]
    const trades = [
      trade(41, T + 1_000, 24600), // after T: not shown
      trade(40, bucket + 2_000, 24530),
      trade(39, bucket + 1_000, 24510),
    ]
    const shown = candlesUpTo(stored, trades, T, 24530)
    expect(shown.map((c) => Date.parse(c.bucket_start))).toEqual([bucket - 5_000, bucket])
    expect(shown[1]).toMatchObject({
      open_paise: 24510,
      high_paise: 24530,
      close_paise: 24530,
      trade_count: 2,
      volume: 10,
    })
  })

  it('a bucket with no trade yet at T is flat at the last price', () => {
    const shown = candlesUpTo([], [], T, 24480)
    expect(shown).toEqual([candle(T - (T % 5_000), 24480, 0)])
    expect(candlesUpTo([], [], T, null)).toEqual([])
  })
})

describe('the timeline', () => {
  it('marks the biggest one-minute moves, in time order', () => {
    const minute = (n: number, open: number, close: number): Candle => ({
      ...candle(T + n * 60_000, close),
      open_paise: open,
    })
    const moments = notableMoments(
      [minute(0, 100, 100), minute(1, 100, 130), minute(2, 130, 90), minute(3, 90, 95)],
      2,
    )
    expect(moments).toEqual([
      { at: T + 60_000, change: 30 },
      { at: T + 120_000, change: -40 },
    ])
  })

  it('a replay lasts 30 minutes at most and never reaches now', () => {
    expect(replayEnd(T, T + 3_600_000)).toBe(T + 30 * 60_000)
    expect(replayEnd(T, T + 60_000)).toBe(T + 59_000)
  })
})
