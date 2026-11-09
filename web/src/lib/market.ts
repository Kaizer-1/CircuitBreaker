// The public API's market, order and portfolio calls (api/api-gateway.yaml), and their shapes
// (from api/market-data-service.yaml, api/order-service.yaml, api/portfolio-service.yaml).

export const TICKERS = ['CHAIWALA', 'JUGAAD', 'DABBA', 'MONSOON', 'FILMY'] as const
export type Ticker = (typeof TICKERS)[number]
export type Side = 'BUY' | 'SELL'
export type OrderType = 'LIMIT' | 'MARKET'

export interface Instrument {
  ticker: Ticker
  name: string
  reference_price_paise: number
  last_price_paise: number
  day_open_paise: number
  day_high_paise: number
  day_low_paise: number
  change_paise: number
  change_percent: number
  day_volume: number
  updated_at: string
}
export interface Candle {
  bucket_start: string
  open_paise: number
  high_paise: number
  low_paise: number
  close_paise: number
  volume: number
  trade_count: number
}
export interface Level {
  price_paise: number
  quantity: number
  order_count: number
}
export interface OrderBook {
  ticker: Ticker
  as_of_sequence: number
  as_of: string
  best_bid_paise: number | null
  best_ask_paise: number | null
  spread_paise: number | null
  bids: Level[]
  asks: Level[]
}
export interface Trade {
  trade_id: string
  sequence: number
  price_paise: number
  quantity: number
  aggressor_side: Side
  executed_at: string
}
export interface Holding {
  ticker: Ticker
  quantity: number
  reserved_quantity: number
  available_quantity: number
  avg_cost_paise: number
  last_price_paise: number
  market_value_paise: number
  unrealised_pnl_paise: number
}
export interface Portfolio {
  trader_id: string
  nickname: string
  kind: string
  cash_paise: number
  reserved_cash_paise: number
  available_cash_paise: number
  holdings: Holding[]
  net_worth_paise: number
  starting_value_paise: number
  pnl_paise: number
  pnl_percent: number
  as_of: string
}
export interface Order {
  order_id: string
  ticker: Ticker
  side: Side
  order_type: OrderType
  quantity: number
  limit_price_paise: number | null
  status: 'PENDING' | 'OPEN' | 'PARTIALLY_FILLED' | 'FILLED' | 'CANCELLED' | 'REJECTED'
  filled_quantity: number
  remaining_quantity: number
  cancel_requested?: boolean
  placed_at?: string
}
export interface OrderBody {
  ticker: Ticker
  side: Side
  order_type: OrderType
  quantity: number
  limit_price_paise?: number
}

/** A Problem Details answer (api/README.md error catalog). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message)
  }
}

async function call<T>(
  method: string,
  path: string,
  token: string | null,
  body?: unknown,
  headers: Record<string, string> = {},
): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: {
      Accept: 'application/json',
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    let code = 'HTTP_' + response.status
    let detail = `The exchange answered HTTP ${response.status}.`
    try {
      const problem = (await response.json()) as { code?: string; detail?: string; title?: string }
      code = problem.code ?? code
      detail = problem.detail ?? problem.title ?? detail
    } catch {
      // not Problem Details: keep the generic message
    }
    throw new ApiError(response.status, code, detail)
  }
  return (response.status === 204 ? undefined : await response.json()) as T
}

/** Q-12 (api/market-data-service.yaml TimeTravelView). */
export interface Verification {
  live_sequence: number
  live_state_hash: string
  matches: boolean
}
export interface TimeTravelView {
  ticker: Ticker
  at: string
  as_of_sequence: number
  last_price_paise: number | null
  order_book: OrderBook
  candles: Candle[] // 1-minute, up to `at`
  recent_trades: Trade[] // newest first
  state_hash: string
  verification: Verification | null
}
/** Q-13 messages (api/README.md). */
export interface ReplayFrame {
  as_of: string
  as_of_sequence: number
  order_book: OrderBook
  last_trade: Trade | null
  candle: Candle // the forming 5s candle
}
export interface ReplayEnd {
  frames_sent: number
  from: string
  to: string
  as_of_sequence: number
  state_hash: string
}
export interface LeaderboardEntry {
  rank: number
  trader_id: string
  nickname: string
  kind: 'HUMAN' | 'BOT' | 'LOAD'
  net_worth_paise: number
  pnl_percent: number
}

export interface JoinAnswer {
  access_token: string
  trader_id: string
  nickname: string
  expires_at: string
}

export const api = {
  join: (nickname: string) => call<JoinAnswer>('POST', '/v1/sessions', null, { nickname }),
  instruments: (token: string) =>
    call<{ instruments: Instrument[] }>('GET', '/v1/instruments', token).then((b) => b.instruments),
  candles: (token: string, ticker: Ticker, interval: '5s' | '1m') =>
    call<{ candles: Candle[] }>(
      'GET',
      `/v1/market/${ticker}/candles?interval=${interval}&limit=120`,
      token,
    ).then((b) => b.candles),
  /** Candles in a window, oldest first (Q-03): the time-travel timeline and the replay chart. */
  candlesBetween: (
    token: string,
    ticker: Ticker,
    interval: '5s' | '1m',
    from: string,
    to: string,
    limit: number,
  ) =>
    call<{ candles: Candle[] }>(
      'GET',
      `/v1/market/${ticker}/candles?${new URLSearchParams({ interval, from, to, limit: String(limit) })}`,
      token,
    ).then((b) => b.candles),
  timeTravel: (token: string, ticker: Ticker, at: string) =>
    call<TimeTravelView>(
      'GET',
      `/v1/time-travel/${ticker}?${new URLSearchParams({ at, depth: '6' })}`,
      token,
    ),
  leaderboard: (token: string, limit = 10) =>
    call<{ as_of: string; entries: LeaderboardEntry[] }>(
      'GET',
      `/v1/leaderboard?limit=${limit}`,
      token,
    ),
  portfolio: (token: string) => call<Portfolio>('GET', '/v1/portfolio', token),
  openOrders: (token: string) =>
    call<{ items: Order[] }>('GET', '/v1/orders?status=open&limit=50', token).then((b) => b.items),
  place: (token: string, order: OrderBody, idempotencyKey: string) =>
    call<Order>('POST', '/v1/orders', token, order, { 'Idempotency-Key': idempotencyKey }),
  cancel: (token: string, orderId: string) => call<Order>('DELETE', `/v1/orders/${orderId}`, token),
}
