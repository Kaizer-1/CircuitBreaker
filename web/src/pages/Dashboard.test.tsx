// The presenter dashboard's leaderboard: on by default, the presenter can take it off the
// projector (button or key B), the choice is remembered, and a hidden board is not even polled.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { api } from '../lib/market'
import { emptyMarket } from '../market/marketState'
import { BOTH, showTheme } from '../../test/themes'
import { Dashboard } from './Dashboard'

vi.mock('../components/CandleChart', () => ({ CandleChart: () => <div data-testid="chart" /> }))
vi.mock('./presenterHooks', () => ({
  useNow: () => new Date('2026-11-20T05:12:15.000Z'),
  useHealth: () => 'all systems healthy',
  useRowsThatFit: (_panel: unknown, max: number) => max,
}))
const feed = vi.hoisted(() => ({ state: null as unknown }))
vi.mock('../incidents/hooks', async () => {
  const { emptyFeed } = await import('../lib/incidents')
  return { useIncidentFeed: () => [feed.state ?? emptyFeed, 'live'] }
})
vi.mock('../market/hooks', async (original) => {
  const real = await original<typeof import('../market/hooks')>()
  const market = () => [emptyMarket, 'live']
  return {
    ...real,
    useMarkets: () => ({
      CALVERA: market(),
      KORVIN: market(),
      ASTRYX: market(),
      HELIOR: market(),
      TAVIRA: market(),
    }),
    useInstruments: () => [],
  }
})

const KEY = 'circuitbreaker.leaderboard'
const entries = [
  {
    rank: 1,
    trader_id: 'a',
    nickname: 'MeeraMoon',
    kind: 'HUMAN',
    net_worth_paise: 10_425_050,
    pnl_percent: 4.25,
  },
]

beforeEach(() => {
  sessionStorage.setItem('circuitbreaker.admin_token', 'admin-token')
  localStorage.removeItem(KEY)
  vi.spyOn(api, 'leaderboard').mockResolvedValue({
    as_of: '2026-11-20T05:12:14Z',
    entries,
  } as never)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe.each(BOTH)('the dashboard leaderboard (%s theme)', (theme) => {
  beforeEach(() => showTheme(theme))

  it('is shown by default with the humans ranked, and the watchlist makes room for it', async () => {
    render(<Dashboard />)
    expect(await screen.findByText('MeeraMoon')).toBeInTheDocument()
    expect(api.leaderboard).toHaveBeenCalledWith('admin-token', 5)
    expect(screen.getByRole('region', { name: 'Watchlist' })).toHaveClass('compact')
  })

  it('the presenter hides it with the button, the choice is remembered, and it is no longer polled', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      render(<Dashboard />)
      // The label says what the button does (like the theme toggle).
      fireEvent.click(screen.getByRole('button', { name: 'Hide leaderboard' }))
      expect(screen.queryByRole('region', { name: 'Leaderboard' })).toBeNull()
      expect(screen.getByRole('button', { name: 'Show leaderboard' })).toBeInTheDocument()
      expect(localStorage.getItem(KEY)).toBe('hidden')
      expect(screen.getByRole('region', { name: 'Watchlist' })).not.toHaveClass('compact')
      const polls = vi.mocked(api.leaderboard).mock.calls.length
      await act(() => vi.advanceTimersByTimeAsync(10_000))
      expect(vi.mocked(api.leaderboard).mock.calls.length).toBe(polls)

      cleanup()
      render(<Dashboard />) // a reload: still hidden
      expect(screen.queryByRole('region', { name: 'Leaderboard' })).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('key B shows and hides it', () => {
    render(<Dashboard />)
    fireEvent.keyDown(window, { key: 'b' })
    expect(screen.queryByRole('region', { name: 'Leaderboard' })).toBeNull()
    fireEvent.keyDown(window, { key: 'B' })
    expect(screen.getByRole('region', { name: 'Leaderboard' })).toBeInTheDocument()
  })
})

describe.each(BOTH)('the dashboard status bar (%s skin)', (theme) => {
  beforeEach(() => showTheme(theme))
  afterEach(() => {
    feed.state = null
  })

  it('shows the open incident in one line, and nothing once there is none', async () => {
    const { applyFeed, emptyFeed } = await import('../lib/incidents')
    const opened = {
      incident_id: 'INC-0007',
      alert_name: 'OrderServiceHighErrorRate',
      service: 'order-service',
      severity: 'critical' as const,
      status: 'OPEN' as const,
      resolution: null,
      summary: 'order-service errors at 38%',
      opened_at: '2026-11-20T05:12:00.000Z',
      resolved_at: null,
      time_to_recover_seconds: null,
      actions_taken: [],
    }
    let state = applyFeed(emptyFeed, { type: 'incident_opened', data: opened }, 0)
    state = applyFeed(
      state,
      {
        type: 'incident_update',
        data: {
          incident_id: 'INC-0007',
          entry: {
            at: '2026-11-20T05:12:09.000Z',
            kind: 'EXECUTED',
            message: 'Rolled order-service back to revision 7. The rollout has started.',
          },
        },
      },
      0,
    )
    feed.state = state
    render(<Dashboard />)
    expect(screen.getByTestId('incident-line')).toHaveTextContent(
      'INC-0007 order-service: action: Rolled order-service back to revision 7',
    )
    cleanup()
    feed.state = null
    render(<Dashboard />)
    expect(screen.queryByTestId('incident-line')).toBeNull()
  })
})
