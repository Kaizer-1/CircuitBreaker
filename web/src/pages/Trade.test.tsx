// The phone's order flow where it really runs: plain http://<LAN IP>, where the browser offers no
// crypto.randomUUID. An order must still be sent (with a v4 Idempotency-Key), and a failure must say
// why on screen and log the whole error to the console.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { saveTrader } from '../lib/session'
import { emptyMarket } from '../market/marketState'
import { BOTH, showTheme } from '../../test/themes'
import { Trade } from './Trade'

vi.mock('../components/CandleChart', () => ({ CandleChart: () => <div data-testid="chart" /> }))
vi.mock('../market/hooks', () => ({
  useMarket: () => [{ ...emptyMarket, lastPrice: 24_785 }, 'live'],
  useInstruments: () => [],
  usePortfolio: () => [
    {
      trader_id: 't',
      nickname: 'Raju',
      kind: 'HUMAN',
      cash_paise: 10_000_000,
      reserved_cash_paise: 0,
      available_cash_paise: 10_000_000,
      holdings: [],
      net_worth_paise: 10_000_000,
      starting_value_paise: 10_000_000,
      pnl_paise: 0,
      pnl_percent: 0,
      as_of: '2026-11-14T10:00:00Z',
    },
    'live',
  ],
}))

const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const json = (status: number, body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status }))

/** fetch as the gateway answers it; `place` answers POST /v1/orders. */
function exchange(place: () => Promise<Response>) {
  const fetch = vi.fn((_url: string, init?: RequestInit) =>
    init?.method === 'POST' ? place() : json(200, { items: [] }),
  )
  vi.stubGlobal('fetch', fetch)
  return fetch
}
const buy = () => fireEvent.click(screen.getByRole('button', { name: /BUY 10 CHAIWALA/ }))

beforeEach(() => {
  // The phone's origin: no randomUUID (a secure-context-only API), getRandomValues as everywhere.
  const real = globalThis.crypto
  vi.stubGlobal('crypto', { getRandomValues: real.getRandomValues.bind(real) })
  saveTrader({ token: 'tok', traderId: 't', nickname: 'Raju', expiresAt: '2099-01-01T00:00:00Z' })
})
afterEach(() => {
  cleanup()
  saveTrader(null)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe.each(BOTH)('placing an order from a phone on http (%s theme)', (theme) => {
  beforeEach(() => showTheme(theme))

  it('sends the order with a v4 Idempotency-Key although crypto.randomUUID is undefined', async () => {
    const fetch = exchange(() => json(202, { order_id: 'o1', status: 'PENDING' }))
    render(<Trade />)
    buy()
    expect(await screen.findByRole('status')).toHaveTextContent('Order sent: BUY 10 CHAIWALA')
    const [, init] = fetch.mock.calls.find(([, i]) => i?.method === 'POST')!
    expect(JSON.parse(init!.body as string)).toMatchObject({ ticker: 'CHAIWALA', side: 'BUY' })
    expect((init!.headers as Record<string, string>)['Idempotency-Key']).toMatch(V4)
  })

  it.each([
    [
      'no connection',
      () => Promise.reject(new TypeError('Failed to fetch')),
      'No connection to the exchange. Check the Wi-Fi and try again.',
    ],
    [
      'the server refuses',
      () => json(422, { code: 'VALIDATION_FAILED', detail: 'Not enough cash.' }),
      'Not enough cash.',
    ],
    [
      'the exchange fails',
      () => json(503, { code: 'DEPENDENCY_UNAVAILABLE', detail: 'portfolio-service is down' }),
      'The exchange had a problem (HTTP 503). Please try again in a moment.',
    ],
  ])('when %s, says so and logs the error', async (_case, answer, message) => {
    exchange(answer)
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<Trade />)
    buy()
    expect(await screen.findByRole('status')).toHaveTextContent(message)
    expect(log).toHaveBeenCalledWith('Placing the order failed:', expect.anything())
  })

  it('a bug in the page shows a plain message, never the exception, and logs it', async () => {
    vi.stubGlobal('crypto', {}) // no random source at all: the page itself throws
    const fetch = exchange(() => json(202, {}))
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<Trade />)
    buy()
    const toast = await screen.findByRole('status')
    expect(toast).toHaveTextContent('Something went wrong in the app. Please try again.')
    expect(toast).not.toHaveTextContent(/TypeError|undefined|function/)
    expect(log).toHaveBeenCalledWith('Placing the order failed:', expect.any(TypeError))
    expect(fetch.mock.calls.some(([, i]) => i?.method === 'POST')).toBe(false)
  })
})
