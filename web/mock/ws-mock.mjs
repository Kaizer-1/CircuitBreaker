// The WebSocket channels for `make mock` (Prism mocks HTTP only): Q-06 /v1/ws/market/{ticker} and
// Q-10 /v1/ws/portfolio, with the messages api/README.md documents and a market that moves.
// Shapes follow api/market-data-service.yaml and api/portfolio-service.yaml; the values are made up.
// Run: node web/mock/ws-mock.mjs   (listens on :4011; `npm run dev:mock` proxies /v1/ws/ to it)
import { createServer } from 'node:http'
import { randomUUID } from 'node:crypto'
import { WebSocketServer } from 'ws'

const PORT = Number(process.env.MOCK_WS_PORT ?? 4011)
const REFERENCE = { CHAIWALA: 24550, JUGAAD: 118000, DABBA: 41275, MONSOON: 8820, FILMY: 53005 }
const tick = (p) => Math.max(5, Math.round(p / 5) * 5)
const iso = (ms) => new Date(ms).toISOString()
const bucket = (ms) => ms - (ms % 5000)

// One simulated market per ticker: a random walk with trades, a book around the price, candles.
const markets = Object.fromEntries(
  Object.entries(REFERENCE).map(([ticker, ref]) => {
    const now = Date.now()
    const candles = []
    let price = ref
    for (let i = 59; i >= 0; i--) {
      const open = price
      const close = tick(open * (1 + (Math.random() - 0.5) * 0.004))
      candles.push({
        bucket_start: iso(bucket(now) - i * 5000).replace('.000Z', 'Z'),
        open_paise: open,
        high_paise: Math.max(open, close) + 5,
        low_paise: Math.min(open, close) - 5,
        close_paise: close,
        volume: Math.floor(Math.random() * 400),
        trade_count: Math.floor(Math.random() * 12),
      })
      price = close
    }
    return [ticker, { ticker, price, sequence: 1000, candles, trades: [], subscribers: new Set() }]
  }),
)

function book(m) {
  const level = (i, side) => ({
    price_paise: tick(m.price * (1 + (side === 'BUY' ? -1 : 1) * (0.0008 + i * 0.0006))),
    quantity: 5 + Math.floor(Math.random() * 120),
    order_count: 1 + Math.floor(Math.random() * 4),
  })
  const bids = Array.from({ length: 10 }, (_, i) => level(i, 'BUY'))
  const asks = Array.from({ length: 10 }, (_, i) => level(i, 'SELL'))
  return {
    ticker: m.ticker,
    as_of_sequence: m.sequence,
    as_of: iso(Date.now()),
    best_bid_paise: bids[0].price_paise,
    best_ask_paise: asks[0].price_paise,
    spread_paise: asks[0].price_paise - bids[0].price_paise,
    bids,
    asks,
  }
}

function forming(m) {
  const last = m.candles.at(-1)
  const start = iso(bucket(Date.now())).replace('.000Z', 'Z')
  if (last.bucket_start !== start) {
    send(m, { type: 'candle', data: { interval: '5s', closed: true, candle: last } })
    m.candles.push({
      bucket_start: start,
      open_paise: last.close_paise,
      high_paise: last.close_paise,
      low_paise: last.close_paise,
      close_paise: last.close_paise,
      volume: 0,
      trade_count: 0,
    })
    m.candles = m.candles.slice(-60)
  }
  return m.candles.at(-1)
}

function trade(m) {
  m.price = tick(
    m.price * (1 + (Math.random() - 0.5) * 0.002 + (REFERENCE[m.ticker] - m.price) * 1e-6),
  )
  m.sequence += 2
  const t = {
    trade_id: randomUUID(),
    sequence: m.sequence,
    price_paise: m.price,
    quantity: 1 + Math.floor(Math.random() * 40),
    aggressor_side: Math.random() < 0.5 ? 'BUY' : 'SELL',
    executed_at: iso(Date.now()),
  }
  const c = forming(m)
  c.high_paise = Math.max(c.high_paise, t.price_paise)
  c.low_paise = Math.min(c.low_paise, t.price_paise)
  c.close_paise = t.price_paise
  c.volume += t.quantity
  c.trade_count += 1
  m.trades = [t, ...m.trades].slice(0, 50)
  send(m, { type: 'trade', data: t })
}

function send(m, message) {
  const text = JSON.stringify(message)
  for (const ws of m.subscribers) if (ws.readyState === ws.OPEN) ws.send(text)
}

for (const m of Object.values(markets)) {
  const again = () => setTimeout(() => (trade(m), again()), 300 + Math.random() * 900)
  again()
  setInterval(() => send(m, { type: 'book', data: book(m) }), 250) // at most 4 per second
  setInterval(
    () => send(m, { type: 'candle', data: { interval: '5s', closed: false, candle: forming(m) } }),
    1000,
  )
}

// The caller's portfolio (the Q-09 example's shape), with an occasional fill.
function portfolio() {
  const chai = markets.CHAIWALA.price
  return {
    trader_id: '3d2a1b0c-9e8f-4a7b-8c6d-5e4f3a2b1c0d',
    nickname: 'RajuTrader',
    kind: 'HUMAN',
    cash_paise: 9509000,
    reserved_cash_paise: 0,
    available_cash_paise: 9509000,
    holdings: [
      {
        ticker: 'CHAIWALA',
        quantity: 20,
        reserved_quantity: 0,
        available_quantity: 20,
        avg_cost_paise: 24550,
        last_price_paise: chai,
        market_value_paise: 20 * chai,
        unrealised_pnl_paise: 20 * (chai - 24550),
      },
    ],
    net_worth_paise: 9509000 + 20 * chai,
    starting_value_paise: 10000000,
    pnl_paise: 9509000 + 20 * chai - 10000000,
    pnl_percent: Math.round(((9509000 + 20 * chai - 10000000) / 100000) * 100) / 100,
    as_of: iso(Date.now()),
  }
}

const server = createServer((_, res) => res.writeHead(404).end())
const wss = new WebSocketServer({ server })
wss.on('connection', (ws, request) => {
  const path = new URL(request.url, 'http://mock').pathname
  const ping = setInterval(() => ws.send(JSON.stringify({ type: 'ping' })), 15000)
  ws.on('close', () => clearInterval(ping))
  const market = path.match(/^\/v1\/ws\/market\/([A-Z]+)$/)
  if (market) {
    const m = markets[market[1]]
    if (!m) return ws.close(4404, 'unknown ticker')
    ws.send(
      JSON.stringify({
        type: 'snapshot',
        data: {
          order_book: book(m),
          last_price_paise: m.price,
          candles: m.candles,
          recent_trades: m.trades,
        },
      }),
    )
    m.subscribers.add(ws)
    ws.on('close', () => m.subscribers.delete(ws))
    return
  }
  if (path === '/v1/ws/portfolio') {
    ws.send(JSON.stringify({ type: 'portfolio', data: portfolio() }))
    const pushes = setInterval(
      () => ws.send(JSON.stringify({ type: 'portfolio', data: portfolio() })),
      2000,
    )
    ws.on('close', () => clearInterval(pushes))
    return
  }
  ws.close(4404, 'no such channel in the mock')
})
server.listen(PORT, () => console.log(`WebSocket mock on ws://localhost:${PORT} (Q-06, Q-10)`))
