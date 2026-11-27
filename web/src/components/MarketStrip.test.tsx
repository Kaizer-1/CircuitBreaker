// The control room's market strip in both skins: the agent's numbers while fresh, in the alarm
// colour past an alert's threshold, and greyed out with "stale since <time>" once no vitals have
// come for 15 s (owner, 2026-11-27): never old numbers that look current.
import { cleanup, render, screen, within } from '@testing-library/react'
import { applyFeed, emptyFeed, VITALS_STALE_MS } from '../lib/incidents'
import type { Instrument } from '../lib/market'
import { BOTH, showTheme } from '../../test/themes'
import { MarketStrip } from './MarketStrip'

const RECEIVED = Date.parse('2026-11-27T09:30:05.000Z') // 15:00:05 IST
const VITALS = {
  at: '2026-11-27T09:30:05.000Z',
  service: 'order-service',
  requests_per_second: 71.5,
  error_ratio: 0.2072,
  p95_seconds: 2.31,
  replicas: { desired: 5, ready: 3 },
  market_data_memory_of_limit: 0.27,
}
const CALVERA: Instrument = {
  ticker: 'CALVERA',
  name: 'Calvera Consumer Ltd',
  reference_price_paise: 24550,
  last_price_paise: 23100,
  day_open_paise: 24550,
  day_high_paise: 24700,
  day_low_paise: 22900,
  change_paise: -1450,
  change_percent: -5.91,
  day_volume: 1200,
  updated_at: '2026-11-27T09:30:05.000Z',
}
const feed = applyFeed(emptyFeed, { type: 'vitals', data: VITALS }, RECEIVED)

afterEach(cleanup)

describe.each(BOTH)('the market strip (%s skin)', (theme) => {
  beforeEach(() => showTheme(theme))

  test('fresh vitals show what the agent sees, past a threshold in the alarm colour', () => {
    render(<MarketStrip instruments={[CALVERA]} feed={feed} now={RECEIVED + 3_000} />)
    expect(screen.getByText('CALVERA')).toBeInTheDocument()
    expect(screen.getByText('▼−5.91%')).toHaveClass('down')
    const vitals = screen.getByTestId('vitals')
    expect(vitals).not.toHaveClass('stale')
    expect(within(vitals).getByText('2.31 s')).toHaveClass('down') // over 500 ms
    expect(within(vitals).getByText('20.7%')).toHaveClass('down') // over 5%
    expect(within(vitals).getByText('3/5')).toBeInTheDocument()
    expect(within(vitals).getByText('27%')).not.toHaveClass('down') // under 70%
    expect(screen.queryByRole('status')).toBeNull()
  })

  test('after 15 s without vitals the numbers grey out and say since when', () => {
    render(<MarketStrip instruments={[CALVERA]} feed={feed} now={RECEIVED + VITALS_STALE_MS + 1} />)
    const vitals = screen.getByTestId('vitals')
    expect(vitals).toHaveClass('stale')
    expect(within(vitals).getByRole('status')).toHaveTextContent('stale since 15:00:05')
    expect(vitals).toHaveAccessibleName('What the agent sees: stale')
  })

  test('before the first vitals the strip says so', () => {
    render(<MarketStrip instruments={[]} feed={emptyFeed} now={RECEIVED} />)
    expect(screen.getByRole('status')).toHaveTextContent('no vitals yet')
  })
})
