// The time-travel screen in every state and both skins: the past is never shown without the
// historical banner and a tag on every panel; live has neither; the proof shows when it is known.
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { api } from '../lib/market'
import { emptyMarket } from '../market/marketState'
import { initialState, type Mode, type TimeTravelState } from '../timetravel/state'
import type { TimeTravel as Actions } from '../timetravel/useTimeTravel'
import { BOTH, showTheme } from '../../test/themes'
import { TimeTravel } from './TimeTravel'

const AT = Date.parse('2026-11-20T05:12:15.000Z') // 10:42:15 IST
const NOW = new Date('2026-11-20T06:00:00.000Z')

const fake = vi.hoisted(() => ({ state: null as unknown, actions: {} as Record<string, unknown> }))
vi.mock('../timetravel/useTimeTravel', () => ({
  useTimeTravel: () => ({ state: fake.state, ...fake.actions }),
}))
vi.mock('../components/CandleChart', () => ({ CandleChart: () => <div data-testid="chart" /> }))
vi.mock('../market/hooks', () => ({
  useMarket: () => [emptyMarket, 'live'],
  useInstruments: () => [],
}))
vi.mock('./presenterHooks', () => ({
  useNow: () => NOW,
  useHealth: () => 'all systems healthy',
  useRowsThatFit: (_panel: unknown, max: number) => max,
}))

const proof = { live_sequence: 8794, live_state_hash: 'f'.repeat(64), matches: true }

function stateIn(mode: Mode): TimeTravelState {
  if (mode === 'live') return { ...initialState, verification: proof }
  return {
    ...initialState,
    mode,
    target: AT,
    view:
      mode === 'seeking'
        ? null
        : {
            at: AT,
            sequence: 8794,
            book: null,
            lastPrice: 24570,
            candles: [],
            trades: [],
            stateHash: 'f'.repeat(64),
          },
    verification: mode === 'paused' ? proof : null,
  }
}

function show(mode: Mode) {
  const actions = {
    seek: vi.fn(),
    seekNow: vi.fn(),
    verify: vi.fn(),
    play: vi.fn(),
    pause: vi.fn(),
    setSpeed: vi.fn(),
    backToLive: vi.fn(),
  } satisfies Omit<Actions, 'state'>
  fake.state = stateIn(mode)
  fake.actions = actions
  render(<TimeTravel />)
  return actions
}

const PANELS = ['Timeline', 'CHAIWALA chart', 'Order book', 'Trades']

beforeEach(() => {
  sessionStorage.setItem('circuitbreaker.admin_token', 'admin-token')
  vi.spyOn(api, 'candlesBetween').mockResolvedValue([])
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe.each(BOTH)('time travel (%s theme)', (theme) => {
  beforeEach(() => showTheme(theme))

  it.each<Mode>(['seeking', 'paused', 'playing', 'ended'])(
    '%s: the banner and every panel say this is the past',
    (mode) => {
      show(mode)
      const banner = screen.getByRole('status', { name: 'Historical mode' })
      const own = theme === 'paper' ? 'From the archives' : 'HISTORICAL MODE'
      const other = theme === 'paper' ? 'HISTORICAL MODE' : 'From the archives'
      expect(within(banner).getByText(new RegExp(own))).toBeVisible()
      expect(within(banner).getByText(new RegExp(other))).not.toBeVisible()
      expect(banner).toHaveTextContent('10:42:15')
      for (const name of PANELS) {
        const panel = screen.getByRole('region', { name })
        expect(within(panel).getByTestId('history-tag'), name).toHaveTextContent('10:42:15')
      }
      expect(screen.queryByRole('status', { name: 'Live' })).toBeNull()
    },
  )

  it('Back to live is on the banner and goes back to live', () => {
    const actions = show('paused')
    fireEvent.click(screen.getByRole('button', { name: /Back to live/ }))
    expect(actions.backToLive).toHaveBeenCalledTimes(1)
  })

  it('live: no banner, no tags, and the proof from the last rewind to now', () => {
    show('live')
    expect(screen.queryByRole('status', { name: 'Historical mode' })).toBeNull()
    expect(screen.queryAllByTestId('history-tag')).toEqual([])
    const strip = screen.getByRole('status', { name: 'Live' })
    expect(within(strip).getByTestId('proof')).toHaveTextContent(
      /Verified at sequence 8,794: hashes match.*ffffffffffff/,
    )
    // The hero names the sequence its hash belongs to: never a bare hash beside another number.
    expect(screen.getByTestId('state-hash')).toHaveTextContent('verified at 8,794 · ffffffffffff…')
  })

  it('a rewind to now shows the proof next to the controls; a mismatch would say so', () => {
    show('paused')
    expect(screen.getByTestId('proof')).toHaveTextContent(
      'Verified at sequence 8,794: hashes match',
    )
    cleanup()
    fake.state = { ...stateIn('paused'), verification: { ...proof, matches: false } }
    render(<TimeTravel />)
    expect(screen.getByTestId('proof')).toHaveTextContent('Hashes differ at sequence 8,794')
  })

  it.each<[Mode, string]>([
    ['seeking', 'rebuilding…'],
    ['paused', 'ffffffffffff…'],
    ['playing', 'computed when paused'],
    ['ended', 'ffffffffffff…'],
  ])('%s: the state hash says what it is', (mode, text) => {
    show(mode)
    expect(screen.getByTestId('state-hash')).toHaveTextContent(text)
    expect(screen.getByRole('status', { name: 'Historical mode' })).toHaveTextContent(
      mode === 'seeking' ? 'Rebuilding' : '8,794',
    )
  })

  it('Verify against live checks without leaving what is shown', () => {
    const actions = show('paused')
    fireEvent.click(screen.getByRole('button', { name: 'Verify against live' }))
    expect(actions.verify).toHaveBeenCalledTimes(1)
    expect(actions.seekNow).not.toHaveBeenCalled()
  })

  it('Space plays and pauses, L goes back to live', () => {
    const actions = show('paused')
    fireEvent.keyDown(window, { key: ' ' })
    expect(actions.play).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(window, { key: 'l' })
    expect(actions.backToLive).toHaveBeenCalledTimes(1)
  })
})
